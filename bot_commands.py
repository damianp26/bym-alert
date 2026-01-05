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
FEES_FILE = Path("fees.json")


def tg_api(method: str):
    return f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def send_message(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID env vars")

    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    r = requests.post(tg_api("sendMessage"), json=payload, timeout=20)
    r.raise_for_status()


def fetch_cauciones_ars_best_by_days(day_min: int, day_max: int):
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
    data = r.json()

    ars = [x for x in data if x.get("denominationCcy") == "ARS"]

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

    return best_by_days


def parse_amount(s: str) -> float:
    # allow 1_000_000 or 1.000.000 or 1000000
    clean = s.replace("_", "").replace(".", "").replace(",", ".")
    return float(clean)


def format_money(x: float) -> str:
    # simple formatting (ARS)
    return f"${x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_date_ars(maturity_date: str) -> str:
    try:
        dt = datetime.strptime(maturity_date, "%Y-%m-%d")
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return maturity_date or "?"


def calc_return(amount: float, days: int, rate_annual_pct: float, fees_cfg: dict):
    basis = int(fees_cfg.get("day_basis", 365))

    # Gross interest (simple approximation using annual nominal rate)
    gross_interest = amount * (rate_annual_pct / 100.0) * (days / basis)

    broker_rate = float(fees_cfg.get("broker_commission_rate", 0.0))
    iva_rate = float(fees_cfg.get("iva_rate", 0.0))
    market_rate = float(fees_cfg.get("market_fee_rate", 0.0))

    broker_total_rate = broker_rate * (1.0 + iva_rate)
    total_cost = amount * (broker_total_rate + market_rate)

    net_interest = gross_interest - total_cost
    net_total = amount + net_interest

    return {
        "gross_interest": gross_interest,
        "total_cost": total_cost,
        "net_interest": net_interest,
        "net_total": net_total,
        "broker_total_rate": broker_total_rate,
        "market_rate": market_rate,
        "basis": basis,
    }


def handle_command(text: str):
    thresholds_cfg = load_json(THRESHOLDS_FILE, {"day_min": 1, "day_max": 30, "thresholds": {}})
    fees_cfg = load_json(FEES_FILE, {
        "broker_commission_rate": 0.0015,
        "iva_rate": 0.21,
        "market_fee_rate": 0.0,
        "day_basis": 365
    })

    day_min = int(thresholds_cfg.get("day_min", 1))
    day_max = int(thresholds_cfg.get("day_max", 30))
    thresholds = thresholds_cfg.get("thresholds", {})
    thresholds = {str(k): float(v) for k, v in thresholds.items()}

    parts = text.strip().split()
    cmd = parts[0].lower()

    if cmd in ("/help", "help"):
        send_message(
            "**Comandos disponibles**\n"
            "• /thresholds\n"
            "• /set <dias> <umbral>\n"
            "• /unset <dias>\n"
            "• /calc <monto> <dias> [tasa]\n"
            "• /fees\n"
            "\nEjemplos:\n"
            "• /set 7 55\n"
            "• /calc 3000000 7\n"
            "• /calc 3000000 7 60"
        )
        return

    if cmd == "/fees":
        broker = float(fees_cfg.get("broker_commission_rate", 0.0))
        iva = float(fees_cfg.get("iva_rate", 0.0))
        market = float(fees_cfg.get("market_fee_rate", 0.0))
        total = broker * (1 + iva) + market
        send_message(
            f"**Fees**\n"
            f"Broker: {broker*100:.4f}%\n"
            f"IVA sobre broker: {iva*100:.2f}%\n"
            f"Market fee: {market*100:.4f}%\n"
            f"Total sobre monto (aprox): {total*100:.4f}%"
        )
        return

    if cmd == "/thresholds":
        if not thresholds:
            send_message("No hay umbrales cargados aún. Usá: /set <dias> <umbral>")
            return
        lines = []
        for k in sorted(thresholds.keys(), key=lambda x: int(x)):
            lines.append(f"• {k} días → {thresholds[k]:.2f}%")
        send_message("**Umbrales cargados**\n" + "\n".join(lines))
        return

    if cmd == "/set":
        if len(parts) != 3:
            send_message("Uso: /set <dias> <umbral>\nEj: /set 7 55")
            return
        d = int(parts[1])
        v = float(parts[2])
        if d < day_min or d > day_max:
            send_message(f"El día debe estar entre {day_min} y {day_max}.")
            return
        thresholds[str(d)] = v
        thresholds_cfg["thresholds"] = thresholds
        save_json(THRESHOLDS_FILE, thresholds_cfg)
        send_message(f"✅ Umbral seteado: {d} días → {v:.2f}%")
        return

    if cmd == "/unset":
        if len(parts) != 2:
            send_message("Uso: /unset <dias>\nEj: /unset 7")
            return
        d = str(int(parts[1]))
        if d in thresholds:
            thresholds.pop(d)
            thresholds_cfg["thresholds"] = thresholds
            save_json(THRESHOLDS_FILE, thresholds_cfg)
            send_message(f"✅ Umbral eliminado para {d} días.")
        else:
            send_message(f"No había umbral para {d} días.")
        return

    if cmd == "/calc":
        if len(parts) not in (3, 4):
            send_message("Uso: /calc <monto> <dias> [tasa]\nEj: /calc 3000000 7\nEj: /calc 3000000 7 60")
            return

        amount = parse_amount(parts[1])
        days = int(parts[2])

        # rate: optional; if absent, use BYMA best rate for that day
        if len(parts) == 4:
            rate = float(parts[3])
            vto = "?"
        else:
            best_by_days = fetch_cauciones_ars_best_by_days(day_min, day_max)
            row = best_by_days.get(days)
            if not row:
                send_message(f"No encontré tasa ARS para {days} días ahora mismo.")
                return
            rate = float(row.get("settlementPrice", 0.0))
            vto = format_date_ars(row.get("maturityDate", ""))

        out = calc_return(amount, days, rate, fees_cfg)
        send_message(
            f"**📌 Cálculo caución ARS 🚨**\n"
            f"Monto: {format_money(amount)}\n"
            f"Plazo: {days} días\n"
            f"Vto: {vto}\n"
            f"Caución Colocadora (TNA aprox): {rate:.2f}%\n\n"
            f"Interés bruto: {format_money(out['gross_interest'])}\n"
            f"Costos (broker+IVA{'+mercado' if out['market_rate']>0 else ''}): {format_money(out['total_cost'])}\n"
            f"Interés neto estimado: {format_money(out['net_interest'])}\n"
            f"Total estimado al vencimiento: {format_money(out['net_total'])}\n"
            f"📌 Base: {out['basis']} días"
        )
        return

    send_message("No entendí el comando. Probá /help")


def main():
    # load state and get last update offset
    state = load_json(STATE_FILE, {})
    offset = int(state.get("_telegram_offset", 0))

    # getUpdates
    params = {"offset": offset, "timeout": 0}
    r = requests.get(tg_api("getUpdates"), params=params, timeout=20)
    r.raise_for_status()
    updates = r.json().get("result", [])

    changed = False

    for upd in updates:
        update_id = int(upd.get("update_id", 0))
        message = upd.get("message") or upd.get("edited_message") or {}
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))

        # only accept your chat
        if chat_id != str(CHAT_ID):
            # still advance offset to avoid re-reading
            state["_telegram_offset"] = update_id + 1
            changed = True
            continue

        text = (message.get("text") or "").strip()
        if text:
            handle_command(text)

        # advance offset
        state["_telegram_offset"] = update_id + 1
        changed = True

    if changed:
        save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
