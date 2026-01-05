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
RULES_FILE = Path("rules.json")  # your JSON

# --- Anti-spam tuning ---
COOLDOWN_MINUTES = 15
MIN_IMPROVEMENT = 0.10  # if rate increases by >= this, notify again even within cooldown


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


def escape_md_v2(text: str) -> str:
    # Telegram MarkdownV2 reserved characters:
    # _ * [ ] ( ) ~ ` > # + - = | { } . !
    specials = r"_*[]()~`>#+-=|{}.!"
    out = ""
    for ch in text:
        if ch in specials:
            out += "\\" + ch
        else:
            out += ch
    return out


def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID env vars")

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": escape_md_v2(text),
        "parse_mode": "MarkdownV2",
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


def load_rules():
    if not RULES_FILE.exists():
        raise FileNotFoundError("rules.json not found in repo root.")

    cfg = json.loads(RULES_FILE.read_text(encoding="utf-8"))

    cost_rate = float(cfg.get("cost_rate", 0.0))
    base_days = int(cfg.get("base_days", 365))
    capital_rules = cfg.get("capital_rules", [])

    normalized = []
    for r in capital_rules:
        if not r.get("enabled", False):
            continue
        thresholds = r.get("thresholds", {})
        thresholds = {str(k): float(v) for k, v in thresholds.items()}
        normalized.append({
            "capital_min": float(r["capital_min"]),
            "capital_max": float(r["capital_max"]),
            "day_min": int(r.get("day_min", 1)),
            "day_max": int(r.get("day_max", 30)),
            "min_net_profit": float(r.get("min_net_profit", 0.0)),
            "thresholds": thresholds,
        })

    return cost_rate, base_days, normalized


def format_date_ars(maturity_date: str) -> str:
    try:
        dt = datetime.strptime(maturity_date, "%Y-%m-%d")
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return maturity_date or "?"


def format_money_ars(x: float) -> str:
    return f"${x:,.0f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_money_ars2(x: float) -> str:
    return f"${x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def should_notify(state: dict, key: str, rate: float) -> bool:
    entry = state.get(key, {})
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
    state[key] = {"last_sent_ts": time.time(), "last_sent_rate": rate}


def net_profit(amount: float, days: int, annual_rate_pct: float, base_days: int, cost_rate: float) -> float:
    gross = amount * (annual_rate_pct / 100.0) * (days / base_days)
    cost = amount * cost_rate
    return gross - cost


def required_capital_for_profit(min_profit: float, days: int, annual_rate_pct: float, base_days: int, cost_rate: float):
    per_peso = (annual_rate_pct / 100.0) * (days / base_days) - cost_rate
    if per_peso <= 0:
        return None
    return min_profit / per_peso


def main():
    cost_rate, base_days, capital_rules = load_rules()

    data = fetch_cauciones()
    ars = [x for x in data if x.get("denominationCcy") == "ARS"]

    # best offer per day (max settlementPrice) for days 1..30
    best_by_days = {}
    for x in ars:
        d = x.get("daysToMaturity")
        if d is None:
            continue
        d = int(d)
        if d < 1 or d > 30:
            continue
        rate = float(x.get("settlementPrice", 0.0))
        if d not in best_by_days or rate > float(best_by_days[d].get("settlementPrice", 0.0)):
            best_by_days[d] = x

    state = load_state()

    day_sections = []
    changed = False
    triggered_any = False

    # Track best opportunity overall (by max net at capital_max)
    best_pick = None  # dict with keys: days, rate, vto, cap, net, rule_min, rule_max

    for days, row in sorted(best_by_days.items()):
        rate = float(row.get("settlementPrice", 0.0))
        vto = format_date_ars(row.get("maturityDate", ""))

        matching_rules_lines = []

        for rule in capital_rules:
            if days < rule["day_min"] or days > rule["day_max"]:
                continue

            threshold = rule["thresholds"].get(str(days))
            if threshold is None:
                continue

            if rate < threshold:
                continue

            cap_min = rule["capital_min"]
            cap_max = rule["capital_max"]
            min_profit = rule["min_net_profit"]

            net_min = net_profit(cap_min, days, rate, base_days, cost_rate)
            net_max = net_profit(cap_max, days, rate, base_days, cost_rate)

            # If even at cap_max you can't reach min profit, skip this rule
            if net_max < min_profit:
                continue

            # Determine "desde cuánto" meets min_net_profit
            desde = None
            if min_profit > 0:
                req = required_capital_for_profit(min_profit, days, rate, base_days, cost_rate)
                if req is not None:
                    desde = max(cap_min, min(cap_max, req))

            range_label = f"{format_money_ars(cap_min)}–{format_money_ars(cap_max)}"
            profit_label = f"Neto {format_money_ars2(net_min)}–{format_money_ars2(net_max)}"

            if min_profit > 0:
                if net_min >= min_profit:
                    extra = f"✅ (min {format_money_ars(min_profit)} ok)"
                else:
                    extra = f"⚠️ (min {format_money_ars(min_profit)} desde {format_money_ars(desde) if desde else 'N/A'})"
            else:
                extra = ""

            matching_rules_lines.append(f"• Capital {range_label} → {profit_label} {extra}")

            # Update best pick using cap_max (max expected net in that rule)
            if best_pick is None or net_max > best_pick["net"]:
                best_pick = {
                    "days": days,
                    "rate": rate,
                    "vto": vto,
                    "cap": cap_max,
                    "net": net_max,
                    "cap_range": (cap_min, cap_max),
                    "min_profit": min_profit,
                    "threshold": threshold,
                }

        if not matching_rules_lines:
            continue

        key = f"ARS_{days}"
        if not should_notify(state, key, rate):
            continue

        triggered_any = True
        update_state(state, key, rate)
        changed = True

        section = (
            f"**{days} días** | Vto: {vto} | **Caución Colocadora ARS:** {rate:.2f}%\n"
            + "\n".join(matching_rules_lines)
        )
        day_sections.append(section)

    if triggered_any:
        header = "**🚨 Oportunidades de Caución Colocadora (ARS) 🚨**"

        best_block = ""
        if best_pick is not None:
            cap_min, cap_max = best_pick["cap_range"]
            best_block = (
                "\n\n"
                "**🏆 Mejor neto estimado (usando capital_max del rango)**\n"
                f"• Plazo: **{best_pick['days']} días** | Vto: {best_pick['vto']} | Tasa: {best_pick['rate']:.2f}%\n"
                f"• Rango: {format_money_ars(cap_min)}–{format_money_ars(cap_max)} (usando {format_money_ars(best_pick['cap'])})\n"
                f"• Neto estimado: **{format_money_ars2(best_pick['net'])}**\n"
            )

        footer = f"\n📌 Costos usados: {cost_rate*100:.4f}% sobre monto | Base: {base_days} días\n🚨"

        msg = header + "\n\n" + "\n\n".join(day_sections) + best_block + footer
        send_telegram(msg)

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
