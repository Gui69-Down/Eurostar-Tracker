"""
Eurostar weekend price tracker — v4
-----------------------------------
Changes vs v3:
  - Searches ROUND TRIPS instead of one-ways. Eurostar prices the same leg
    differently depending on search type, and since the real use case is a
    Fri->Sun weekend return, we now query with both outbound and inbound
    dates so tracked prices match what you'd actually pay.
  - Each weekend produces two trips (LDN->PAR->LDN and PAR->LDN->PAR), and
    each trip is scraped twice: once for the outbound page, once for the
    return page (same URL + "&direction=inbound").
  - config.json must now contain "roundtrip_url_template", e.g.:
      "https://www.eurostar.com/search/fr-fr?adult=1&origin={origin}&destination={destination}&outbound={outbound}&inbound={inbound}"
Kept from v3:
  - PRICE_RE matches both "£39"/"€39.00" and French "234 €"/"39,00 €".
Kept from v2:
  - Departure times validated by pairing with a plausible arrival time
    (travel duration +/- timezone shift per direction).
  - Prices below 29 rejected; narrow price search zone next to each journey.
"""

import json
import re
import sys
import time
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())
HISTORY_PATH = ROOT / "data" / "history.json"
DEBUG_DIR = ROOT / "data" / "debug"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Matches "£39", "€39.00" (symbol first) AND "234 €", "39,00 €" (symbol last,
# as rendered on the fr-fr market — \s also covers the non-breaking space).
PRICE_RE = re.compile(
    r"(?:[£€]\s*(\d{1,3}(?:[.,]\d{2})?)"
    r"|(\d{1,3}(?:[.,]\d{2})?)\s*[£€])"
)
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")

PRICE_MIN, PRICE_MAX = 29, 500

# Displayed gap (minutes) between departure and arrival local times, per direction.
# LDN->PAR: ~2h16-2h35 travel + 1h timezone shift  -> 170-235 min displayed
# PAR->LDN: ~2h16-2h35 travel - 1h timezone shift  ->  55-115 min displayed
DISPLAY_GAP = {
    "LDN-PAR": (170, 235),
    "PAR-LDN": (55, 115),
}


# ---------------------------------------------------------------- dates

def upcoming_fridays(weeks: int) -> list[date]:
    today = date.today()
    days_to_friday = (4 - today.weekday()) % 7
    first = today + timedelta(days=days_to_friday or 7)
    return [first + timedelta(weeks=w) for w in range(weeks)]


# ---------------------------------------------------------------- helpers

def _within_window(hhmm: str, window: dict) -> bool:
    return window["earliest"] <= hhmm <= window["latest"]


def _to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _plausible_pair(dep: str, arr: str, direction: str) -> bool:
    lo, hi = DISPLAY_GAP[direction]
    gap = (_to_min(arr) - _to_min(dep)) % (24 * 60)
    return lo <= gap <= hi


def _price_from_match(pm) -> float:
    raw = pm.group(1) or pm.group(2)
    return float(raw.replace(",", "."))


def extract_min_price_from_json(payloads: list, window: dict, direction: str):
    """Walk captured JSON payloads for journey objects carrying a departure
    time and a price. Where an arrival time is present, validate the pair."""
    best = None

    def walk(node):
        nonlocal best
        if isinstance(node, dict):
            price = None
            for k in ("price", "amount", "totalPrice", "lowestPrice", "adultPrice", "value"):
                v = node.get(k)
                if isinstance(v, (int, float)) and PRICE_MIN <= v < PRICE_MAX:
                    price = float(v)
                    break
                if isinstance(v, dict):
                    inner = v.get("amount") or v.get("value")
                    if isinstance(inner, (int, float)) and PRICE_MIN <= inner < PRICE_MAX:
                        price = float(inner)
                        break
            dep = arr = None
            for k in ("departureTime", "departure", "departAt", "departureDateTime"):
                v = node.get(k)
                if isinstance(v, str):
                    m = TIME_RE.search(v)
                    if m:
                        dep = f"{int(m.group(1)):02d}:{m.group(2)}"
                        break
            for k in ("arrivalTime", "arrival", "arriveAt", "arrivalDateTime"):
                v = node.get(k)
                if isinstance(v, str):
                    m = TIME_RE.search(v)
                    if m:
                        arr = f"{int(m.group(1)):02d}:{m.group(2)}"
                        break
            ok = (price is not None and dep is not None
                  and _within_window(dep, window)
                  and (arr is None or _plausible_pair(dep, arr, direction)))
            if ok and (best is None or price < best["price"]):
                best = {"price": price, "dep": dep, "source": "json"}
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for p in payloads:
        try:
            walk(p)
        except Exception:
            continue
    return best


def extract_min_price_from_text(text: str, window: dict, direction: str):
    """A time only counts as a departure if the NEXT time on the page sits
    close after it and forms a plausible departure/arrival pair for this
    direction. The price is then searched just after the arrival time."""
    best = None
    times = [(m.start(), f"{int(m.group(1)):02d}:{m.group(2)}")
             for m in TIME_RE.finditer(text)]
    for i, (pos, dep) in enumerate(times):
        if not _within_window(dep, window):
            continue
        if i + 1 >= len(times):
            continue
        pos2, arr = times[i + 1]
        if pos2 - pos > 80:               # arrival must sit right next to departure
            continue
        if not _plausible_pair(dep, arr, direction):
            continue
        chunk = text[pos2: pos2 + 160]     # price sits just after the time pair
        pm = PRICE_RE.search(chunk)
        if pm:
            price = _price_from_match(pm)
            if PRICE_MIN <= price < PRICE_MAX and (best is None or price < best["price"]):
                best = {"price": price, "dep": dep, "source": "dom"}
    return best


# ---------------------------------------------------------------- scraping

def scrape_page(page, url: str, d: date, window: dict, label: str, direction: str):
    """Load one search results page (outbound or return leg of a round trip)
    and extract the cheapest fare within the time window."""
    captured = []

    def on_response(resp):
        try:
            ct = resp.headers.get("content-type", "")
            if "json" in ct and any(
                k in resp.url.lower()
                for k in ("search", "journey", "train", "fare", "price", "availab")
            ):
                captured.append(resp.json())
        except Exception:
            pass

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        for sel in ("#onetrust-accept-btn-handler",
                    "button:has-text('Accept')",
                    "button:has-text('Accepter')"):
            try:
                page.locator(sel).first.click(timeout=2500)
                break
            except Exception:
                continue
        page.wait_for_timeout(9000)
        result = extract_min_price_from_json(captured, window, direction)
        if result is None:
            body = page.inner_text("body")
            result = extract_min_price_from_text(body, window, direction)
        if result is None:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            (DEBUG_DIR / f"{label}_{d.isoformat()}.txt").write_text(
                page.inner_text("body")[:20000]
            )
            print(f"  !! no price found for {label} {d} (debug dump saved)")
        else:
            print(f"  {label} {d}: {result['price']} at {result['dep']} ({result['source']})")
        return result
    finally:
        page.remove_listener("response", on_response)


def main():
    stations = CONFIG["stations"]
    fridays = upcoming_fridays(CONFIG["weeks_ahead"])
    history = json.loads(HISTORY_PATH.read_text()) if HISTORY_PATH.exists() else {}
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    tmpl = CONFIG["roundtrip_url_template"]

    # Each weekend = two round trips (one starting from each city).
    # Each round trip = two pages: outbound results + return results
    # (same URL with "&direction=inbound").
    tasks = []
    for fri in fridays:
        sun = fri + timedelta(days=2)
        for out_dir in ("LDN-PAR", "PAR-LDN"):
            o, dst = out_dir.split("-")
            back_dir = f"{dst}-{o}"
            base_url = tmpl.format(
                origin=stations[o]["code"],
                destination=stations[dst]["code"],
                outbound=fri.isoformat(),
                inbound=sun.isoformat(),
            )
            tasks.append({
                "url": base_url,
                "direction": out_dir,
                "date": fri,
                "window": CONFIG["legs"]["friday_evening"],
            })
            tasks.append({
                "url": base_url + "&direction=inbound",
                "direction": back_dir,
                "date": sun,
                "window": CONFIG["legs"]["sunday_return"],
            })

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, locale="en-GB",
                                  viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        for t in tasks:
            label = f"{t['direction']}_{t['window']['earliest']}"
            res = scrape_page(page, t["url"], t["date"], t["window"], label, t["direction"])
            if res:
                key = f"{t['date'].isoformat()}_{t['direction']}_{t['window']['earliest']}"
                history.setdefault(key, []).append(
                    {"ts": now, "price": res["price"], "dep": res["dep"]}
                )
            time.sleep(random.uniform(4, 9))
        browser.close()

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, indent=1))
    print(f"Saved history: {len(history)} tracked legs")


if __name__ == "__main__":
    sys.exit(main())
