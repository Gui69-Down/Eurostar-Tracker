"""
Eurostar weekend price tracker — v7
-----------------------------------
Changes vs v6:
  - The debug dump showed the v5/v6 click was hitting the "Eurostar Standard"
    COLUMN HEADER, not a fare: after the click the page still said
    "Aucun train choisi" and remained on the outbound results.
  - Fix 1: fare clicks now target '[aria-label*="Départ de:"][aria-label*=
    "Eurostar Standard"]' — real fare cells carry both fragments in their
    aria-label; the header carries neither.
  - Fix 2: after selecting a fare, click the "Suivant" (choix retour) button
    to actually advance the funnel to the return page.
  - Fix 3: before extracting the return leg, VERIFY the page contains
    "(voyage retour)". If not, dump and skip — never extract from the wrong
    page. This eliminates duplicated outbound prices for good.
Kept from v6:
  - DOM-first extraction; JSON fallback requires a validated arrival time.
Kept from v5:
  - Real booking funnel (no "&direction=inbound" URL).
Kept from v4:
  - Round-trip searches via config "roundtrip_url_template".
Kept from v3:
  - PRICE_RE matches both "£39"/"€39.00" and French "234 €"/"39,00 €".
Kept from v2:
  - Departure/arrival pairing validation; prices below 29 rejected.
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

RETURN_PAGE_MARKER = "voyage retour)"   # appears as "(voyage retour)" only on the return results page


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
    """Walk captured JSON payloads for journey objects. A candidate MUST have
    a departure time in the window, a price, AND an arrival time forming a
    plausible pair for this direction. Nodes without an arrival are rejected
    (they are typically basket/selection payloads, not search results)."""
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
            ok = (price is not None and dep is not None and arr is not None
                  and _within_window(dep, window)
                  and _plausible_pair(dep, arr, direction))
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

def _extract_current(page, captured, d, window, label, direction):
    """Extract the cheapest in-window fare from whatever the page shows now.
    DOM first (rendered price = bookable price), JSON as fallback."""
    body = page.inner_text("body")
    result = extract_min_price_from_text(body, window, direction)
    if result is None:
        result = extract_min_price_from_json(captured, window, direction)
    if result is None:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (DEBUG_DIR / f"{label}_{d.isoformat()}.txt").write_text(body[:20000])
        print(f"  !! no price found for {label} {d} (debug dump saved)")
    else:
        print(f"  {label} {d}: {result['price']} at {result['dep']} ({result['source']})")
    return result


def _dismiss_popups(page):
    """Best-effort dismissal of cookie banners and upsell modals."""
    for sel in ("#onetrust-accept-btn-handler",
                "button:has-text('Accepter')",
                "button:has-text('Accept')",
                "button:has-text('Non merci')",
                "button:has-text('No thanks')",
                "button:has-text('Continuer sans')"):
        try:
            page.locator(sel).first.click(timeout=1500)
        except Exception:
            continue


def _dump(page, name):
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    (DEBUG_DIR / f"{name}.txt").write_text(page.inner_text("body")[:20000])


def _open_return_page(page) -> bool:
    """Select a Standard fare (real fare cells carry both 'Départ de:' and
    'Eurostar Standard' in their aria-label), then click 'Suivant' to reach
    the return results page. Returns True once '(voyage retour)' is shown."""
    # 1) select a fare
    fare_clicked = False
    for sel in ('[aria-label*="Départ de:"][aria-label*="Eurostar Standard"]',
                '[aria-label*="Départ de"][aria-label*="Standard"]'):
        try:
            page.locator(sel).first.click(timeout=6000)
            fare_clicked = True
            break
        except Exception:
            continue
    if not fare_clicked:
        return False
    page.wait_for_timeout(2000)
    _dismiss_popups(page)

    # 2) advance to the return step (button may be labelled "Suivant : choix retour")
    for sel in ("button:has-text('Suivant')",
                "button:has-text('choix retour')",
                "a:has-text('Suivant')"):
        try:
            page.locator(sel).first.click(timeout=4000)
            break
        except Exception:
            continue
    _dismiss_popups(page)

    # 3) wait until the return page marker appears (up to ~15s)
    for _ in range(15):
        page.wait_for_timeout(1000)
        try:
            if RETURN_PAGE_MARKER in page.inner_text("body"):
                return True
        except Exception:
            continue
    return False


def scrape_trip(page, trip):
    """One round trip = outbound results page, then advance the funnel to the
    verified return results page, extract both legs."""
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

    out_res = ret_res = None
    page.on("response", on_response)
    try:
        # ---- outbound leg
        page.goto(trip["url"], wait_until="domcontentloaded", timeout=60000)
        _dismiss_popups(page)
        page.wait_for_timeout(9000)
        out_label = f"{trip['out_dir']}_{trip['out_window']['earliest']}"
        out_res = _extract_current(page, captured, trip["out_date"],
                                   trip["out_window"], out_label, trip["out_dir"])

        # ---- advance to verified return page
        captured.clear()
        ret_label = f"{trip['ret_dir']}_{trip['ret_window']['earliest']}_RET"
        if not _open_return_page(page):
            _dump(page, f"{ret_label}_{trip['ret_date'].isoformat()}_wrongpage")
            print(f"  !! return page not reached for {ret_label} "
                  f"{trip['ret_date']} (debug dump saved)")
            return out_res, None

        page.wait_for_timeout(7000)
        ret_res = _extract_current(page, captured, trip["ret_date"],
                                   trip["ret_window"], ret_label, trip["ret_dir"])
        return out_res, ret_res
    finally:
        page.remove_listener("response", on_response)


def main():
    stations = CONFIG["stations"]
    fridays = upcoming_fridays(CONFIG["weeks_ahead"])
    history = json.loads(HISTORY_PATH.read_text()) if HISTORY_PATH.exists() else {}
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    tmpl = CONFIG["roundtrip_url_template"]

    trips = []
    for fri in fridays:
        sun = fri + timedelta(days=2)
        for out_dir in ("LDN-PAR", "PAR-LDN"):
            o, dst = out_dir.split("-")
            trips.append({
                "url": tmpl.format(
                    origin=stations[o]["code"],
                    destination=stations[dst]["code"],
                    outbound=fri.isoformat(),
                    inbound=sun.isoformat(),
                ),
                "out_dir": out_dir,
                "out_date": fri,
                "out_window": CONFIG["legs"]["friday_evening"],
                "ret_dir": f"{dst}-{o}",
                "ret_date": sun,
                "ret_window": CONFIG["legs"]["sunday_return"],
            })

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, locale="en-GB",
                                  viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        for trip in trips:
            out_res, ret_res = scrape_trip(page, trip)
            if out_res:
                key = (f"{trip['out_date'].isoformat()}_{trip['out_dir']}"
                       f"_{trip['out_window']['earliest']}")
                history.setdefault(key, []).append(
                    {"ts": now, "price": out_res["price"], "dep": out_res["dep"]}
                )
            if ret_res:
                key = (f"{trip['ret_date'].isoformat()}_{trip['ret_dir']}"
                       f"_{trip['ret_window']['earliest']}")
                history.setdefault(key, []).append(
                    {"ts": now, "price": ret_res["price"], "dep": ret_res["dep"]}
                )
            time.sleep(random.uniform(4, 9))
        browser.close()

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, indent=1))
    print(f"Saved history: {len(history)} tracked legs")


if __name__ == "__main__":
    sys.exit(main())
