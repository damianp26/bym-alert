import os
import json
import time
import requests
import urllib3
from datetime import datetime
from pathlib import Path

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BYMA_URL = "https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free/cauciones"
PAYLOAD = {"excludeZeroPxAndQty": True}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

BYMA_VERIFY_SSL = os.getenv("BYMA_VERIFY_SSL", "false").lower() == "true"

STATE_FILE = Path("state_actions.json")
THRESHOLDS_FILE = Path("thresholds.json")

# --- Anti-spam tuning ---
COOLDOWN_MINUTES = 15
MIN_IMPROVEMENT = 0.10

# --- Return estimate settings ---
ASSUMED_AMOUNT = 100000.0          # ARS per triggered day
DAY_BASIS = 365                    # 365 by default
BROKER_COMMISSION = 0.0015         # 0.15%
IVA_RATE = 0.21                    # 21% IVA on broker commission
MARKET_FEE_RATE = 0.0              # optional extra fee on amount (set if you know it)


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
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_thresholds():
    if not THRESHOLDS_FILE.exists():
        raise FileNotFoundError("thresholds.json not found in repo root.")

    cfg = json.loads(THRESHOLDS_FILE.read_text(encoding="utf-8"))
    day_min = int(cfg.get("day_min", 1))
    day_max = int(cfg.get("day_max", 30))
    thresholds = cfg.get("thresholds", {})
    thresholds = {str(k): float(v) for k, v in thresholds.items()}
    return day_min, day_max, thresholds


def format_date_ars(maturity_date: str) -> str:
    try:
        dt = datetime.strptime(maturity_date, "%Y-%m-%d")
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return maturity_date or "?"


def format_money_ars(x: float) -> str:
    # e.g., 100000 -> $100.000,00
    return f"${x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def should_notify(state: dict, key: str, rate: float) -> bool:
    entry = state.get(key, {})
    # backward compatibility if older formats exist
    if isinstance(entry, (int, float)):
        last_ts = 0.0
        last_rate = float(entry)
    else:
        last_ts = float(entry.get("last_sent_ts", 0))
        last_rate = float(entry.get("last_sent_rate", 0))

    now = time.time()
    cooldown_ok = (now - last_ts) >= (COOLDOWN_MINUTES * 60)
    improved_enough = rate >= (last_rate + MIN_IMPROVEMENT)
    return cooldown_ok or improved_enough


def update_state(state: dict, key: str, rate: float):
    state[key] = {
        "last_sent_ts": time.time(),
        "last_sent_rate": rate,
    }


def estimate_net_return(amount: float, days: int, annual_rate_pct: float) -> dict:
    gross_interest = amount * (annual_rate_pct / 100.0) * (days / DAY_BASIS)
    broker_total_rate = BROKER_COMMISSION * (1.0 + IVA_RATE)  # 0.15% * 1.21 = 0.1815%
    total_cost = amount * (broker_total_rate + MARKET_FEE_RATE)
    net_interest = gross_interest - total_cost
    return {
        "gross_interest": gross_interest,
        "total_cost": total_cost,
        "net_interest": net_interest,
        "net_total": amount + net_interest,
        "broker_total_rate": broker_total_rate,
    }


def main():
    data = fetch_cauciones()
    ars = [x for x in data if x.get("denominationCcy") == "ARS"]

    day_min, day_max, thresholds = load_thresholds()

    # best offer per day (max settlementPrice)
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

    alert_lines = []
    calc_lines = []
    changed = False
    triggered_any = False

    for days, row in sorted(best_by_days.items()):
        threshold = thresholds.get(str(days))
        if threshold is None:
            continue

        rate = float(row.get("settlementPrice", 0.0))
        if rate < threshold:
            continue

        key = f"ARS_{days}"
        if not should_notify(state, key, rate):
            continue

        vto = format_date_ars(row.get("maturityDate", ""))

        # 1) Alert message line
        alert_lines.append(f"• {days} días | Vto: {vto} | Caución Colocadora: {rate:.2f}%")
        triggered_any = True

        # 2) Calculation line for ARS 100,000
        est = estimate_net_return(ASSUMED_AMOUNT, days, rate)
        calc_lines.append(
            f"• {days} días @ {rate:.2f}% → Neto aprox: {format_money_ars(est['net_interest'])} "
            f"(bruto {format_money_ars(est['gross_interest'])} - costos {format_money_ars(est['total_cost'])})"
        )

        update_state(state, key, rate)
        changed = True

    # Send messages (2 messages) only if something triggered
    if triggered_any:
        title = "**🚨 CAUCIÓN COLOCADORA ARS 🚨**"
        msg1 = title + "\n" + "\n".join(alert_lines) + "\n🚨"
        send_telegram(msg1)

        msg2_title = "**📌 Estimación colocando $100.000 en cada plazo**"
        note = (
            f"\n(Base {DAY_BASIS} días; comisión+IVA aprox {BROKER_COMMISSION*(1+IVA_RATE)*100:.4f}% sobre monto"
            f"{'' if MARKET_FEE_RATE==0 else f' + mercado {MARKET_FEE_RATE*100:.4f}%'}.)"
        )
        msg2 = msg2_title + "\n" + "\n".join(calc_lines) + note
        send_telegram(msg2)

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
