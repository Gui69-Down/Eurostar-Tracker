"""
Builds the weekend report (docs/data.json for the dashboard) and sends
Telegram alerts when a weekend total drops below the threshold or falls
sharply versus the previous reading.

Env vars required for alerts:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import json
import os
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())
HISTORY = json.loads((ROOT / "data" / "history.json").read_text()) \
    if (ROOT / "data" / "history.json").exists() else {}
STATE_PATH = ROOT / "data" / "alert_state.json"
OUT_PATH = ROOT / "docs" / "data.json"

FRI_W = CONFIG["legs"]["friday_evening"]["earliest"]
SUN_W = CONFIG["legs"]["sunday_return"]["earliest"]


def latest(key):
    entries = HISTORY.get(key, [])
    return entries[-1] if entries else None


def previous(key):
    entries = HISTORY.get(key, [])
    return entries[-2] if len(entries) >= 2 else None


def series(key):
    return [{"ts": e["ts"], "price": e["price"]} for e in HISTORY.get(key, [])]


def build_weekends():
    """One row per (friday, home_city). home=LDN means weekend a Paris."""
    fridays = sorted({k.split("_")[0] for k in HISTORY
                      if k.endswith(FRI_W) and date.fromisoformat(k.split("_")[0]).weekday() == 4})
    rows = []
    for fri_s in fridays:
        fri = date.fromisoformat(fri_s)
        if fri < date.today():
            continue
        sun = (fri + timedelta(days=2)).isoformat()
        for home, out_dir, back_dir in (("LDN", "LDN-PAR", "PAR-LDN"),
                                        ("PAR", "PAR-LDN", "LDN-PAR")):
            out_key = f"{fri_s}_{out_dir}_{FRI_W}"
            back_key = f"{sun}_{back_dir}_{SUN_W}"
            o, b = latest(out_key), latest(back_key)
            if not (o and b):
                continue
            total = round(o["price"] + b["price"], 2)
            po, pb = previous(out_key), previous(back_key)
            prev_total = round(po["price"] + pb["price"], 2) if (po and pb) else None
            rows.append({
                "friday": fri_s, "sunday": sun, "home": home,
                "out": {"dep": o["dep"], "price": o["price"]},
                "back": {"dep": b["dep"], "price": b["price"]},
                "total": total, "prev_total": prev_total,
                "history": {"out": series(out_key), "back": series(back_key)},
            })
    rows.sort(key=lambda r: (r["home"], r["friday"]))
    return rows


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("Telegram not configured, skipping alert:", text[:80])
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20)


def run_alerts(rows):
    cfg = CONFIG["alerts"]
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    now = datetime.now(timezone.utc)
    for r in rows:
        key = f"{r['friday']}_{r['home']}"
        reasons = []
        if r["total"] <= cfg["weekend_total_threshold"]:
            reasons.append(f"sous le seuil ({cfg['weekend_total_threshold']})")
        if r["prev_total"] and r["total"] < r["prev_total"]:
            drop = (1 - r["total"] / r["prev_total"]) * 100
            if drop >= cfg["drop_pct_trigger"]:
                reasons.append(f"-{drop:.0f}% vs derniere lecture ({r['prev_total']})")
        if not reasons:
            continue
        last = state.get(key)
        if last:
            last_ts = datetime.fromisoformat(last["ts"])
            recent = (now - last_ts) < timedelta(hours=cfg["min_hours_between_same_alert"])
            if recent and r["total"] >= last["total"]:
                continue  # already alerted at this level recently
        dest = "Paris" if r["home"] == "LDN" else "Londres"
        msg = (f"🚄 <b>Eurostar — weekend a {dest}</b>\n"
               f"Ven {r['friday']} {r['out']['dep']} → Dim {r['sunday']} {r['back']['dep']}\n"
               f"<b>Total: {r['total']}</b> ({r['out']['price']} + {r['back']['price']})\n"
               f"Raison: {', '.join(reasons)}\n"
               f"https://www.eurostar.com")
        send_telegram(msg)
        state[key] = {"ts": now.isoformat(), "total": r["total"]}
        print("ALERT:", key, r["total"], reasons)
    STATE_PATH.write_text(json.dumps(state, indent=1))


def main():
    rows = build_weekends()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "currency": CONFIG.get("currency_hint", "GBP"),
        "threshold": CONFIG["alerts"]["weekend_total_threshold"],
        "weekends": rows,
    }, indent=1))
    print(f"Report: {len(rows)} weekend rows")
    run_alerts(rows)


if __name__ == "__main__":
    main()
