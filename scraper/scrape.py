"""
Eurostar weekend price tracker — v9
-----------------------------------
Changes vs v8:
  - Fixed departure-time mislabelling: the price search window after a
    departure/arrival pair could overrun into the NEXT journey card when the
    current card had no matching price nearby (sold-out class, upsell badge),
    attributing the next train's price to the wrong departure time (prices
    were correct, labels sometimes off by one card).
  - The search window is now truncated at the next time occurrence on the
    page, so a price can only be attributed to the card it belongs to.
Kept from v8:
  - Ladder of verified fare-selection strategies + step telemetry.
Kept from v7:
  - Return page verified via "(voyage retour)" before extraction.
Kept from v6:
  - DOM-first extraction; JSON fallback requires a validated arrival time.
Kept from v4/v3/v2:
  - Round-trip URLs; FR/GB price formats; departure/arrival pairing
    validation; prices below 29 rejected.
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

RETURN_PAGE_MARKER = "voyage retour)"   # "(voyage retour)" only on the return results page
NO_TRAIN_MARKER = "Aucun train choisi"  # shown in the basket while nothing is selected


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
    direction. The price is searched just after the arrival time, but never
    beyond the next time on the page — so a price can only be attributed to
    the journey card it belongs to."""
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
        chunk_end = pos2 + 160             # price sits just after the time pair...
        if i + 2 < len(times):
            chunk_end = min(chunk_end, times[i + 2][0])   # ...but never past the next card
        chunk = text[pos2: chunk_end]
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


def _fare_selected(page) -> bool:
    """Selection succeeded once the basket no longer says 'Aucun train choisi'."""
    try:
        return NO_TRAIN_MARKER not in page.inner_text("body")
    except Exception:
        return False


def _select_standard_fare(page) -> str | None:
    """Try a ladder of strategies to select a Standard fare. Returns the name
    of the strategy that verifiably selected a train, else None."""
    name_re = re.compile(r"Départ de.*Eurostar Standard")

    def try_click(clickable):
        try:
            clickable.first.click(timeout=5000)
            page.wait_for_timeout(2500)
            _dismiss_popups(page)
            return _fare_selected(page)
        except Exception:
            return False

    strategies = [
        ("role=button+name", lambda: page.get_by_role("button", name=name_re)),
        ("role=radio+name", lambda: page.get_by_role("radio", name=name_re)),
        ("role=option+name", lambda: page.get_by_role("option", name=name_re)),
        ("button:has-text", lambda: page.locator("button:has-text('Eurostar Standard')")
                                        .filter(has_text="Départ de")),
        ("label:has-text", lambda: page.locator("label:has-text('Eurostar Standard')")
                                       .filter(has_text="Départ de")),
        ("any:has-text", lambda: page.locator("[class*='fare'], [class*='price'], [class*='class']")
                                     .filter(has_text="Eurostar Standard")
                                     .filter(has_text="Départ de")),
    ]
    for name, make in strategies:
        try:
            loc = make()
            if loc.count() == 0:
                continue
        except Exception:
            continue
        if try_click(loc):
            return name
    return None


def _open_return_page(page) -> bool:
    """Select a Standard fare, press 'Suivant', and confirm we reached the
    '(voyage retour)' page."""
    strategy = _select_standard_fare(page)
    if strategy is None:
        print("     fare selection FAILED (no strategy worked)")
        return False
    print(f"     fare selected via [{strategy}]")

    clicked_next = False
    for sel in ("button:has-text('Suivant')",
                "button:has-text('choix retour')",
                "a:has-text('Suivant')"):
        try:
            page.locator(sel).first.click(timeout=4000)
            clicked_next = True
            break
        except Exception:
            continue
    print(f"     suivant clicked: {clicked_next}")
    _dismiss_popups(page)

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
