"""
Eurostar weekend price tracker
------------------------------
Scrapes eurostar.com for the cheapest fares on:
  - Friday evening   LDN -> PAR   (weekend a Paris)
  - Sunday afternoon PAR -> LDN   (retour)
  - and the reverse combination (weekend a Londres)

Strategy:
  1. Open the public search page for each date/direction with Playwright.
  2. Prefer intercepting the JSON responses the booking app fetches
     (any response containing journey/price data).
  3. Fall back to DOM/text extraction if no JSON is captured.
  4. Store the min price within the configured time window into data/history.json.

NOTE: Eurostar has no public API and uses anti-bot protections. This script
uses a real headless browser and behaves like a normal visitor (2 requests
per day, ~64 pages), but selectors/URLs may need occasional maintenance.
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

PRICE_RE = re.compile(r"[£€]\s?(\d{1,3}(?:[.,]\d{2})?)")
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")


# ---------------------------------------------------------------- dates

def upcoming_fridays(weeks: int) -> list[date]:
    today = date.today()
    days_to_friday = (4 - today.weekday()) % 7
    first = today + timedelta(days=days_to_friday or 7)  # next Friday, not today
    return [first + timedelta(weeks=w) for w in range(weeks)]


# ---------------------------------------------------------------- parsing helpers

def _within_window(hhmm: str, window: dict) -> bool:
    return window["earliest"] <= hhmm <= window["latest"]


def extract_min_price_from_json(payloads: list, window: dict):
    """Walk any captured JSON payloads looking for journey-like objects
    that carry a departure time and a price. Deliberately schema-agnostic
    so it survives minor API changes."""
    best = None

    def walk(node):
        nonlocal best
        if isinstance(node, dict):
            blob = json.dumps(node)[:2000]
            times = TIME_RE.findall(blob)
            # look for common price keys
            price = None
            for k in ("price", "amount", "totalPrice", "lowestPrice", "adultPrice", "value"):
                v = node.get(k)
                if isinstance(v, (int, float)) and 5 < v < 500:
                    price = float(v)
                    break
                if isinstance(v, dict):
                    inner = v.get("amount") or v.get("value")
                    if isinstance(inner, (int, float)) and 5 < inner < 500:
                        price = float(inner)
                        break
            dep = None
            for k in ("departureTime", "departure", "departAt", "departureDateTime"):
                v = node.get(k)
                if isinstance(v, str):
                    m = TIME_RE.search(v)
                    if m:
                        dep = f"{int(m.group(1)):02d}:{m.group(2)}"
                        break
            if price is not None and dep is not None and _within_window(dep, window):
                if best is None or price < best["price"]:
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


def extract_min_price_from_text(text: str, window: dict):
    """Fallback: pair times and prices appearing close together in page text."""
    best = None
    for m in TIME_RE.finditer(text):
        dep = f"{int(m.group(1)):02d}:{m.group(2)}"
        if not _within_window(dep, window):
            continue
        chunk = text[m.end(): m.end() + 220]
        pm = PRICE_RE.search(chunk)
        if pm:
            price = float(pm.group(1).replace(",", "."))
            if 5 < price < 500 and (best is None or price < best["price"]):
                best = {"price": price, "dep": dep, "source": "dom"}
    return best


# ---------------------------------------------------------------- scraping

def scrape_leg(page, origin_code, dest_code, d: date, window: dict, label: str):
    url = CONFIG["search_url_template"].format(
        origin=origin_code, destination=dest_code, date=d.isoformat()
    )
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
        # accept cookie banner if present
        for sel in ("#onetrust-accept-btn-handler",
                    "button:has-text('Accept')",
                    "button:has-text('Accepter')"):
            try:
                page.locator(sel).first.click(timeout=2500)
                break
            except Exception:
                continue
        page.wait_for_timeout(9000)  # let the results app load & fetch
        result = extract_min_price_from_json(captured, window)
        if result is None:
            body = page.inner_text("body")
            result = extract_min_price_from_text(body, window)
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

    legs = []
    for fri in fridays:
        sun = fri + timedelta(days=2)
        # Weekend a Paris: LDN->PAR vendredi soir, PAR->LDN dimanche
        legs.append(("LDN-PAR", fri, CONFIG["legs"]["friday_evening"]))
        legs.append(("PAR-LDN", sun, CONFIG["legs"]["sunday_return"]))
        # Weekend a Londres (inverse)
        legs.append(("PAR-LDN", fri, CONFIG["legs"]["friday_evening"]))
        legs.append(("LDN-PAR", sun, CONFIG["legs"]["sunday_return"]))

    # dedupe (same leg can appear for both weekend directions)
    seen, unique_legs = set(), []
    for direction, d, window in legs:
        key = (direction, d.isoformat(), window["earliest"])
        if key not in seen:
            seen.add(key)
            unique_legs.append((direction, d, window))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, locale="en-GB",
                                  viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        for direction, d, window in unique_legs:
            o, dst = direction.split("-")
            label = f"{direction}_{window['earliest']}"
            res = scrape_leg(page, stations[o]["code"], stations[dst]["code"], d, window, label)
            if res:
                key = f"{d.isoformat()}_{direction}_{window['earliest']}"
                history.setdefault(key, []).append(
                    {"ts": now, "price": res["price"], "dep": res["dep"]}
                )
            time.sleep(random.uniform(4, 9))  # polite pacing
        browser.close()

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, indent=1))
    print(f"Saved history: {len(history)} tracked legs")


if __name__ == "__main__":
    sys.exit(main())
