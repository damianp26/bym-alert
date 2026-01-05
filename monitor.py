import os
import json
import requests
import urllib3
from pathlib import Path

# --- Disable warnings when SSL verification is disabled ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BYMA_URL = "https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free/cauciones"
PAYLOAD = {"excludeZeroPxAndQty": True}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Control SSL verification via env var (GitHub Actions can set this to "false")
BYMA_VERIFY_SSL = os.getenv("BYMA_VERIFY_SSL", "false").lower() == "true"

STATE_FILE = Path("state.json")
THRESHOLDS_FILE = Path("thresholds.json")


def fetch_cauciones():
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://open.bymadata.com.ar",
        "Referer": "https://open.bymadata.com.ar/",
        "User-Agent": "Mozilla/5.0 (compatible; bym-alert/1.0)",
    }
    r = requests.post(
        BYMA_URL,
        json=PAYLOAD,
        headers=headers,
        timeout=20,
        verify=BYMA_VERIFY_SSL,
    )
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


def load_thresholds():
    """
    thresholds.json format:
    {
      "day_min": 1,
      "day_max": 30,
      "thresholds": {
        "1": 80.0,
        "2": 75.0,
        "7": 50.0
      }
    }
    """
    if not THRESHOLDS_FILE.exists():
        raise FileNotFoundError(
            "thresholds.json not found. Create it in repo root with day_min/day_max/thresholds."
        )

    cfg = json.loads(THRESHOLDS_FILE.read_text(encoding="utf-8"))
    day_min = int(cfg.get("day_min", 1))
    day_max = int(cfg.get("day_max", 30))

    thresholds = cfg.get("thresholds", {})
    # Normalize keys to strings and values to float
    thresholds = {str(k): float(v) for k, v in thresholds.items()}
    return day_min, day_max, thresholds


def main():
    data = fetch_cauciones()

    # Only ARS
    ars = [x for x in data if x.get("denominationCcy") == "ARS"]

    day_min, day_max, thresholds = load_thresholds()

    # Keep best (max settlementPrice) per daysToMaturity in range
    best_by_days = {}
    for x in ars:
        d = x.get("daysToMaturity")
        if d is None:
            continue
        d = int(d)
        if d < day_min or d > day_max:
            continue

        rate = float(x.get("settlementPrice", 0.0))
        if d not in best_by_days or rate > float(best_by_days[d].get("settlementPrice", 0.0)):
            best_by_days[d] = x

    state = load_state()
    changed = False

    # Evaluate only days that exist AND have threshold configured
    for days, row in sorted(best_by_days.items()):
        threshold = thresholds.get(str(days))
        if threshold is None:
            continue  # no threshold configured => skip

        rate = float(row.get("settlementPrice", 0.0))
        maturity = row.get("maturityDate", "?")

        key = f"ARS_{days}"
        last_alerted = float(state.get(key, 0.0))

        # Alert only if it meets threshold AND is higher than last alerted (anti-spam)
        if rate >= threshold and rate > last_alerted:
            msg = (
                f"🚨 CAUCIONES ARS\n"
                f"Plazo: {days} días | Vto: {maturity}\n"
                f"Tasa Ult: {rate:.2f}% (umbral {threshold:.2f}%)"
            )
            send_telegram(msg)
            state[key] = rate
            changed = True

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
