import os
import json
import asyncio
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

ROME = ZoneInfo("Europe/Rome")

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Percorso file dati ────────────────────────────────────────────────────────
DATA_FILE = "data.json"

# ── Utilità dati ─────────────────────────────────────────────────────────────

def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"chat_ids": [], "events": []}


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── Parser palinsesto ─────────────────────────────────────────────────────────

def parse_palinsesto(text: str) -> list[dict]:
    """
    Estrae gli eventi dal testo del palinsesto.
    Gestisce qualsiasi formato con orario HH:MM seguito da sport e squadre.
    """
    events = []

    # Pulizia: rimuove bullet points e simboli a inizio riga
    clean_lines = []
    for l in text.splitlines():
        l = l.strip()
        l = re.sub(r"^[•\-\*oO°➤➜▸▶◆◉●]\s*", "", l).strip()
        if l:
            clean_lines.append(l)
    lines = clean_lines

    sel_re = re.compile(r"selezione[:\s]+(.+)", re.IGNORECASE)
    quota_re = re.compile(r"quota[:\s]+([0-9.,]+)", re.IGNORECASE)

    # Riconosce una riga evento: HH:MM - Sport: squadre
    header_re = re.compile(
        r"^(\d{1,2}:\d{2})\s*[-–]\s*"
        r"(Tennis|Basket|Calcio|Volley|Rugby|Football|Baseball|Hockey|Golf|Darts|Snooker|MMA|Boxe|\w+)"
        r"\s*:\s*(.+)$",
        re.IGNORECASE,
    )

    i = 0
    while i < len(lines):
        line = lines[i]

        m = header_re.match(line)
        if m:
            time_str = m.group(1)
            sport = m.group(2).strip()
            teams = m.group(3).strip()

            event = {
                "time": time_str,
                "sport": sport,
                "teams": teams,
                "selezione": "",
                "quota": "",
                "notified": False,
            }

            # Cerca Selezione e Quota nelle righe successive (max 8 righe)
            j = i + 1
            while j < min(i + 9, len(lines)):
                sm = sel_re.search(lines[j])
                qm = quota_re.search(lines[j])
                if sm and not event["selezione"]:
                    event["selezione"] = sm.group(1).strip()
                if qm and not event["quota"]:
                    event["quota"] = qm.group(1).strip()
                j += 1

            events.append(event)
            i += 1
        else:
            i += 1

    return events


# ── Scheduler avvisi ──────────────────────────────────────────────────────────

async def check_events(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Controlla ogni minuto se ci sono eventi a 10 minuti dall'inizio."""
    data = load_data()
    now = datetime.now()
    changed = False

    for event in data["events"]:
        if event.get("notified"):
            continue
        try:
            event_time = datetime.strptime(event["time"], "%H:%M").replace(
                year=now.year, month=now.month, day=now.day
            )
        except ValueError:
            continue

        delta = (event_time - now).total_seconds()

        # Finestra di avviso: tra 9:30 e 10:30 minuti prima
        if 570 <= delta <= 630:
            event["notified"] = True
            changed = True
            msg = (
                f"⏰ *AVVISO — tra 10 minuti!*\n\n"
                f"🏆 *{event['sport']}*\n"
                f"⚔️ {event['teams']}\n"
                f"🕐 Orario: *{event['time']}*\n"
            )
            if event["selezione"]:
                msg += f"✅ Selezione: *{event['selezione']}*\n"
            if event["quota"]:
                msg += f"💰 Quota: *{event['quota']}*"

            for chat_id in data["chat_ids"]:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=msg,
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.error(f"Errore invio messaggio a {chat_id}: {e}")

    if changed:
        save_data(data)


# ── Handlers comandi ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    data = load_data()
    if chat_id not in data["chat_ids"]:
        data["chat_ids"].append(chat_id)
        save_data(data)
        await update.message.reply_text(
            "👋 *Benvenuto nel bot Palinsesto!*\n\n"
            "Il tuo account è stato registrato. "
            "Da ora riceverai gli avvisi 10 minuti prima di ogni evento.\n\n"
            "📋 Comandi disponibili:\n"
            "/palinsesto — invia il testo del palinsesto\n"
            "/lista — mostra gli eventi di oggi\n"
            "/reset — cancella tutti gli eventi\n"
            "/status — stato del bot",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "✅ Sei già registrato!\n\n"
            "Usa /palinsesto per inviare un nuovo palinsesto.",
            parse_mode="Markdown",
        )


async def cmd_palinsesto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args_text = " ".join(context.args) if context.args else ""
    if args_text:
        await _process_palinsesto(update, args_text)
    else:
        await update.message.reply_text(
            "📋 Inviami il testo del palinsesto nel prossimo messaggio.",
        )
        context.user_data["waiting_palinsesto"] = True


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("waiting_palinsesto"):
        context.user_data["waiting_palinsesto"] = False
        await _process_palinsesto(update, update.message.text)
    else:
        await update.message.reply_text(
            "Non ho capito. Usa /palinsesto per inviare un palinsesto."
        )


async def _process_palinsesto(update: Update, text: str) -> None:
    events = parse_palinsesto(text)
    if not events:
        await update.message.reply_text(
            "❌ Non ho trovato eventi nel testo inviato.\n"
            "Assicurati che il formato contenga orari tipo *11:00 - Tennis: ...*",
            parse_mode="Markdown",
        )
        return

    data = load_data()
    existing_keys = {(e["time"], e["teams"]) for e in data["events"]}
    added = 0
    for ev in events:
        key = (ev["time"], ev["teams"])
        if key not in existing_keys:
            data["events"].append(ev)
            existing_keys.add(key)
            added += 1

    save_data(data)

    lines = [f"✅ *{added} eventi caricati:*\n"]
    for ev in events:
        line = f"🕐 {ev['time']} — {ev['sport']}: {ev['teams']}"
        if ev["quota"]:
            line += f" | Quota {ev['quota']}"
        lines.append(line)

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_lista(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    if not data["events"]:
        await update.message.reply_text("📭 Nessun evento caricato.")
        return

    lines = ["📋 *Eventi in programma oggi:*\n"]
    for ev in data["events"]:
        stato = "✅ Notificato" if ev["notified"] else "⏳ In attesa"
        line = f"🕐 {ev['time']} — {ev['sport']}: {ev['teams']} [{stato}]"
        if ev["selezione"]:
            line += f"\n   ✅ {ev['selezione']}"
        if ev["quota"]:
            line += f" | 💰 {ev['quota']}"
        lines.append(line)

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    data["events"] = []
    save_data(data)
    await update.message.reply_text(
        "🔄 *Reset completato!*\n"
        "Tutti gli eventi sono stati cancellati.\n"
        "Usa /palinsesto per caricarne di nuovi.",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    total = len(data["events"])
    notified = sum(1 for e in data["events"] if e["notified"])
    pending = total - notified
    await update.message.reply_text(
        f"📊 *Stato bot:*\n\n"
        f"📅 Eventi totali: {total}\n"
        f"✅ Già notificati: {notified}\n"
        f"⏳ In attesa: {pending}\n"
        f"👥 Utenti registrati: {len(data['chat_ids'])}",
        parse_mode="Markdown",
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise ValueError("Variabile d'ambiente TELEGRAM_TOKEN non impostata!")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("palinsesto", cmd_palinsesto))
    app.add_handler(CommandHandler("lista", cmd_lista))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text))

    app.job_queue.run_repeating(check_events, interval=60, first=10)

    logger.info("Bot avviato e in ascolto...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
