import os
import json
import requests
from pathlib import Path

BYMA_URL = "https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free/cauciones"
PAYLOAD = {"excludeZeroPxAndQty": True}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Configure your thresholds here (ARS only, settlementPrice = Tasa Ult)
RULES = [
    {"days": 1, "min_rate": 80.0},
    {"days": 7, "min_rate": 50.0},
    {"days": 14, "min_rate": 45.0},
]

STATE_FILE = Path("state.json")


def fetch_cauciones():
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://open.bymadata.com.ar",
        "Referer": "https://open.bymadata.com.ar/",
    }
    r = requests.post(BYMA_URL, json=PAYLOAD, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()


def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID env vars")

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=20)
    r.raise_for_status()


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    data = fetch_cauciones()

    # Only ARS
    ars = [x for x in data if x.get("denominationCcy") == "ARS"]

    # Index by daysToMaturity (keep one row per day)
    by_days = {int(x["daysToMaturity"]): x for x in ars if x.get("daysToMaturity") is not None}

    state = load_state()
    changed = False

    for rule in RULES:
        days = rule["days"]
        min_rate = rule["min_rate"]

        row = by_days.get(days)
        if not row:
            continue

        rate = float(row.get("settlementPrice", 0.0))
        maturity = row.get("maturityDate", "?")

        key = f"ARS_{days}"
        last_alerted = float(state.get(key, 0.0))

        # Alert only if it meets threshold and is higher than last alerted value
        if rate >= min_rate and rate > last_alerted:
            msg = (
                f"🚨 CAUCIONES ARS\n"
                f"Plazo: {days} días | Vto: {maturity}\n"
                f"Tasa Ult: {rate:.2f}% (umbral {min_rate:.2f}%)"
            )
            send_telegram(msg)
            state[key] = rate
            changed = True

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
