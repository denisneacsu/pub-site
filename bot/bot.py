#!/usr/bin/env python3
"""
Bot Telegram per aggiornare memphisristopub.it senza toccare il codice.

Gira su una copia dedicata del repository (~/memphis-bot/site) e per ogni
modifica: aggiorna i file, mostra un'anteprima, e dopo la conferma fa
commit + push. GitHub Pages pubblica in 1-2 minuti.

Configurazione in ~/memphis-bot/.env:

    TELEGRAM_TOKEN=...          token da @BotFather
    ALLOWED_USER_IDS=123,456    ID Telegram autorizzati

Comandi: vedi HELP qui sotto.
"""

import asyncio
import difflib
import html
import json
import logging
import re
import shutil
import ssl
import subprocess
import sys
import threading
import unicodedata
from datetime import date, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


SITE = Path(__file__).resolve().parent.parent
BOT_HOME = SITE.parent
UPLOADS = BOT_HOME / "uploads"

LIVE_URLS = ("https://memphisristopub.it", "http://memphisristopub.it")

TZ = ZoneInfo("Europe/Rome")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("memphis-bot")


HELP = """\
<b>Bot del sito MEMPHIS①</b>

📷 <b>Locandina</b>: manda la foto (meglio come <i>file</i>, per la qualità) e rispondi alle domande.
Scorciatoie nella didascalia:
• <code>Halloween 31/10 19:30</code> → evento
• <code>promo Ribs</code> oppure <code>promo Ribs fino 30/11</code> → promozione

📋 /eventi · /promozioni — elenco, con pulsante per togliere

🍔 <b>Menu</b>
• <code>/prezzo spritz 6,50</code>
• <code>/prezzo leffe rouge 4 7</code> (piccola e media)
• <code>/nascondi picanha</code> · <code>/mostra picanha</code>

📢 <code>/avviso Chiuso per ferie dal 10 al 20/8</code> · <code>/avviso off</code>

↩️ /annulla — annulla l'ultima modifica del bot
🕑 /stato — ultime modifiche al sito
✋ /stop — interrompe una locandina a metà

Prima di pubblicare chiedo sempre conferma, con un'anteprima."""


# =========================================================
# CONFIGURAZIONE
# =========================================================

def load_env():

    # Sicurezza: il bot fa "git reset --hard", quindi deve girare solo
    # nella sua copia (~/memphis-bot/site), mai nella cartella di sviluppo.
    if SITE.name != "site" or not (BOT_HOME / ".env").exists():
        raise SystemExit(
            f"Il bot va avviato da ~/memphis-bot/site, non da {SITE}"
        )

    env = {}

    for line in (BOT_HOME / ".env").read_text().splitlines():

        line = line.strip()

        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()

    return env


ENV = load_env()

ALLOWED = {
    int(value)
    for value in ENV.get("ALLOWED_USER_IDS", "").split(",")
    if value.strip()
}

AUTH = filters.User(user_id=ALLOWED)


# =========================================================
# FILE DEL SITO
# =========================================================

def read_json(name):
    return json.loads((SITE / name).read_text())


def write_json(name, data):
    (SITE / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=4) + "\n"
    )


def format_menu(menu):
    """
    menu.json con una voce per riga, così resta leggibile
    a mano e ogni modifica del bot tocca una riga sola.
    """

    def value(v):
        return json.dumps(v, ensure_ascii=False)

    def one_line(item):
        return "{ " + ", ".join(
            f"{value(k)}: {value(v)}" for k, v in item.items()
        ) + " }"

    lines = ["{", f'    "note": {value(menu.get("note", ""))},', "", '    "sections": [']

    for s_index, section in enumerate(menu["sections"]):

        lines += [
            "        {",
            f'            "title": {value(section["title"])},',
            '            "categories": [',
        ]

        for c_index, category in enumerate(section["categories"]):

            lines.append("                {")

            for key, v in category.items():
                if key != "items":
                    lines.append(f"                    {value(key)}: {value(v)},")

            lines.append('                    "items": [')

            items = category["items"]

            for i_index, item in enumerate(items):
                comma = "," if i_index < len(items) - 1 else ""
                lines.append(f"                        {one_line(item)}{comma}")

            lines.append("                    ]")

            comma = "," if c_index < len(section["categories"]) - 1 else ""
            lines.append("                }" + comma)

        lines.append("            ]")

        comma = "," if s_index < len(menu["sections"]) - 1 else ""
        lines.append("        }" + comma)

    lines += ["    ]", "}"]

    return "\n".join(lines) + "\n"


def write_menu(menu):
    (SITE / "menu.json").write_text(format_menu(menu))


# =========================================================
# GIT
# =========================================================

def git(*args):

    result = subprocess.run(
        ["git", *args],
        cwd=SITE,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")

    return result.stdout.strip()


def sync():
    """Allinea la copia del bot a GitHub, scartando residui locali."""

    git("fetch", "--quiet", "origin")
    git("reset", "--quiet", "--hard", "origin/main")
    git("clean", "--quiet", "-fd")


def commit_and_push(message, paths):

    git("add", "-A", "--", *paths)

    if not git("status", "--porcelain", "--", *paths):
        raise RuntimeError("Nessuna modifica da pubblicare.")

    git("commit", "--quiet", "-m", message)

    try:
        git("push", "--quiet", "origin", "main")
    except RuntimeError:
        # Qualcuno ha pubblicato nel frattempo: ci si rimette in coda
        git("pull", "--quiet", "--rebase", "origin", "main")
        git("push", "--quiet", "origin", "main")

    return git("rev-parse", "--short", "HEAD")


# =========================================================
# MODIFICHE
#
# Ogni modifica è un dizionario ("change") che apply_change()
# applica ai file. Si applica due volte: per l'anteprima (poi
# scartata) e alla conferma, su una copia appena allineata.
# =========================================================

def run_optimizer():

    subprocess.run(
        [sys.executable, str(SITE / "tools" / "ottimizza-immagini.py")],
        cwd=SITE,
        check=True,
        capture_output=True,
    )


def published_image(stem):
    """Percorso dell'immagine generata dallo script (.jpg o .png)."""

    for ext in (".jpg", ".png"):
        path = SITE / "images" / "events" / f"{stem}{ext}"
        if path.exists():
            return path.relative_to(SITE).as_posix()

    raise RuntimeError("Immagine non generata.")


def apply_change(change):
    """Applica la modifica e restituisce (messaggio di commit, percorsi)."""

    kind = change["kind"]

    if kind in ("event", "promo"):

        source_dir = SITE / "images-src" / "events"
        source_dir.mkdir(parents=True, exist_ok=True)

        upload = Path(change["upload"])
        shutil.copy(upload, source_dir / f"{change['stem']}{upload.suffix.lower()}")

        run_optimizer()

        image = published_image(change["stem"])

        events = read_json("events.json")

        if kind == "event":
            events.append({
                "date": change["date"],
                "time": change["time"],
                "title": change["title"],
                "image": image,
            })
            message = f"Evento: {change['title']} ({format_date(change['date'])})"
        else:
            events.append({
                "type": "promo",
                "title": change["title"],
                "image": image,
                "until": change["until"],
            })
            message = f"Promozione: {change['title']}"

        write_json("events.json", events)

        return message, ["events.json", "images/events"]


    if kind == "remove":

        events = read_json("events.json")

        removed = [e for e in events if e["image"] == change["image"]]
        events = [e for e in events if e["image"] != change["image"]]

        write_json("events.json", events)

        stem = Path(change["image"]).stem

        (SITE / change["image"]).unlink(missing_ok=True)

        for source in (SITE / "images-src" / "events").glob(f"{stem}.*"):
            source.unlink()

        title = removed[0]["title"] if removed else stem

        return f"Tolto: {title}", ["events.json", "images/events"]


    if kind == "menu":

        menu = read_json("menu.json")

        item = find_item(menu, change["category"], change["name"])

        for key, v in change["set"].items():
            if v is None:
                item.pop(key, None)
            else:
                item[key] = v

        write_menu(menu)

        return f"Menu: {change['summary']}", ["menu.json"]


    if kind == "notice":

        write_json("notice.json", {"text": change["text"]})

        message = f"Avviso: {change['text']}" if change["text"] else "Avviso tolto"

        return message, ["notice.json"]


    raise ValueError(f"Modifica sconosciuta: {kind}")


def find_item(menu, category_id, name):

    for section in menu["sections"]:
        for category in section["categories"]:
            if category["id"] == category_id:
                for item in category["items"]:
                    if item["name"] == name:
                        return item

    raise RuntimeError(f"Voce non trovata: {name}")


# =========================================================
# ANTEPRIMA (server locale + browser senza interfaccia)
# =========================================================

class QuietHandler(SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE), **kwargs)

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def start_preview_server():

    server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)

    threading.Thread(target=server.serve_forever, daemon=True).start()

    return f"http://127.0.0.1:{server.server_address[1]}"


PREVIEW_URL = start_preview_server()


def screenshot(change):
    """Screenshot da telefono della parte di sito toccata dalla modifica."""

    kind = change["kind"]

    if kind == "event":
        page, selector = "index.html", "#eventi"
    elif kind == "promo":
        page, selector = "index.html", "#promozioni"
    elif kind == "menu":
        page, selector = "menu.html", f"#{change['category']}"
    else:
        page, selector = "index.html", None

    with sync_playwright() as p:

        browser = p.chromium.launch()

        tab = browser.new_page(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2,
        )

        tab.goto(f"{PREVIEW_URL}/{page}")
        tab.wait_for_timeout(1200)

        if selector and tab.locator(selector).count() and tab.locator(selector).is_visible():
            image = tab.locator(selector).screenshot()
        else:
            image = tab.screenshot()

        browser.close()

    return image


def preview(change):
    """Applica la modifica, fa lo screenshot e la scarta."""

    sync()

    try:
        apply_change(change)
        return screenshot(change)
    finally:
        sync()

        # La sorgente copiata per l'anteprima non deve restare
        if change["kind"] in ("event", "promo"):
            for source in (SITE / "images-src" / "events").glob(f"{change['stem']}.*"):
                source.unlink()


def publish(change):

    sync()

    message, paths = apply_change(change)

    return commit_and_push(message, paths), paths


# Una modifica alla volta sulla copia del repository
LOCK = asyncio.Lock()


async def in_repo(func, *args):
    async with LOCK:
        return await asyncio.to_thread(func, *args)


# =========================================================
# UTILITÀ TESTO E DATE
# =========================================================

def normalize(text):

    text = unicodedata.normalize("NFKD", text.lower())

    return "".join(c for c in text if not unicodedata.combining(c)).strip()


def slugify(text):

    words = re.findall(r"[a-z0-9]+", normalize(text))

    return "-".join(words)[:30] or "locandina"


def today():
    return datetime.now(TZ).date()


def parse_date(text, future=True):
    """'31/10', '31/10/2026', '31.10.26', 'oggi', 'domani'."""

    text = normalize(text)

    if text == "oggi":
        return today()

    if text == "domani":
        return today() + timedelta(days=1)

    match = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?", text)

    if not match:
        return None

    day, month, year = match.groups()

    year = int(year) if year else today().year

    if year < 100:
        year += 2000

    try:
        result = date(year, int(month), int(day))
    except ValueError:
        return None

    # Senza anno, una data già passata si intende l'anno prossimo
    if future and not match.group(3) and result < today():
        result = result.replace(year=year + 1)

    return result


def parse_time(text):

    match = re.fullmatch(r"(?:ore\s*)?(\d{1,2})(?:[:.](\d{2}))?", normalize(text))

    if not match:
        return None

    hours, minutes = int(match.group(1)), int(match.group(2) or 0)

    if hours > 23 or minutes > 59:
        return None

    return f"{hours:02d}:{minutes:02d}"


def format_date(iso):
    return date.fromisoformat(iso).strftime("%d/%m/%Y")


def parse_prices(tokens):
    """Prezzi in coda al comando: '6,50', '4 7', '- 5'."""

    prices = []

    while tokens and re.fullmatch(r"-|€?\d+(?:[.,]\d{1,2})?€?", tokens[-1]):

        token = tokens.pop().strip("€")

        if token == "-":
            prices.insert(0, None)
        else:
            number = float(token.replace(",", "."))
            prices.insert(0, int(number) if number.is_integer() else number)

    return prices


def format_price(value):
    return "—" if value is None else f"€ {value:.2f}".replace(".", ",")


# =========================================================
# CONFERMA E PUBBLICAZIONE
# =========================================================

CONFIRM_BUTTONS = InlineKeyboardMarkup([[
    InlineKeyboardButton("✅ Pubblica", callback_data="publish"),
    InlineKeyboardButton("❌ Annulla", callback_data="discard"),
]])


async def propose(message, context, change, summary):
    """Manda anteprima e riepilogo, e aspetta la conferma."""

    context.user_data["pending"] = change

    waiting = await message.reply_text("⏳ Preparo l'anteprima…")

    try:
        image = await in_repo(preview, change)
    except Exception as error:
        log.exception("Anteprima fallita")
        context.user_data.pop("pending", None)
        await waiting.edit_text(f"⚠️ Non riesco a preparare l'anteprima: {error}")
        return

    await waiting.delete()

    await message.reply_photo(
        photo=image,
        caption=f"{summary}\n\nPubblico sul sito?",
        reply_markup=CONFIRM_BUTTONS,
        parse_mode="HTML",
    )


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    if query.from_user.id not in ALLOWED:
        return await query.answer()

    await query.answer()

    change = context.user_data.pop("pending", None)

    await query.edit_message_reply_markup(reply_markup=None)

    if query.data == "discard" or change is None:
        text = "Annullato, non ho pubblicato nulla." if change else "Questa anteprima è scaduta."
        return await query.message.reply_text(text)

    status = await query.message.reply_text("⏳ Pubblico…")

    try:
        commit, paths = await in_repo(publish, change)
    except Exception as error:
        log.exception("Pubblicazione fallita")
        return await status.edit_text(f"⚠️ Pubblicazione non riuscita: {error}")

    await status.edit_text(
        f"✅ Pubblicato (modifica {commit}).\n"
        "Sul sito sarà visibile tra 1-2 minuti: ti avviso io."
    )

    watched = next(p for p in paths if p.endswith(".json"))
    expected = json.loads((SITE / watched).read_text())

    context.application.create_task(
        wait_until_live(context, query.message.chat_id, watched, expected)
    )


def fetch_live(path):

    for base in LIVE_URLS:
        try:
            stamp = datetime.now().timestamp()
            with urlopen(f"{base}/{path}?t={stamp}", timeout=15) as response:
                return json.loads(response.read())
        except (ssl.SSLError, OSError, ValueError):
            continue

    return None


async def wait_until_live(context, chat_id, path, expected):
    """Controlla il sito finché il file pubblicato non è aggiornato."""

    for _ in range(36):

        await asyncio.sleep(10)

        if await asyncio.to_thread(fetch_live, path) == expected:
            return await context.bot.send_message(
                chat_id, "🌐 È online: https://memphisristopub.it"
            )

    await context.bot.send_message(
        chat_id,
        "⚠️ Dopo 6 minuti il sito non risulta ancora aggiornato. "
        "Controlla tra poco; se non cambia avvisa Denis.",
    )


# =========================================================
# LOCANDINE: conversazione guidata
# =========================================================

TYPE, TITLE, DATE, TIME, UNTIL = range(5)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = update.message

    if message.photo:
        file = await message.photo[-1].get_file()
        suffix = ".jpg"
    else:
        file = await message.document.get_file()
        suffix = Path(message.document.file_name or "x.jpg").suffix or ".jpg"

    UPLOADS.mkdir(exist_ok=True)

    upload = UPLOADS / f"{message.message_id}{suffix.lower()}"

    await file.download_to_drive(upload)

    draft = {"upload": str(upload)}
    context.user_data["draft"] = draft

    if message.photo:
        await message.reply_text(
            "💡 Per una qualità migliore, la prossima volta mandala come file "
            "(📎 → File) invece che come foto."
        )

    caption = (message.caption or "").strip()

    if caption:
        quick = parse_caption(caption)

        if quick:
            draft.update(quick)
            return await finish_draft(message, context)

    await message.reply_text(
        "È un evento o una promozione?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📅 Evento", callback_data="type:event"),
            InlineKeyboardButton("🏷️ Promozione", callback_data="type:promo"),
        ]]),
    )

    return TYPE


def parse_caption(caption):
    """
    'promo Ribs [fino 30/11]'  -> promozione
    'Halloween 31/10 [19:30]'  -> evento
    """

    promo = re.fullmatch(
        r"promo(?:zione)?\s+(.+?)(?:\s+fino(?:\s+al)?\s+(\S+))?",
        caption,
        re.IGNORECASE,
    )

    if promo:

        until = ""

        if promo.group(2):
            parsed = parse_date(promo.group(2))
            if not parsed:
                return None
            until = parsed.isoformat()

        return {"kind": "promo", "title": promo.group(1).strip(), "until": until}

    event = re.fullmatch(
        r"(.+?)\s+(\d{1,2}[/.]\d{1,2}(?:[/.]\d{2,4})?)(?:\s+(?:ore\s+)?(\d{1,2}(?:[:.]\d{2})?))?",
        caption,
    )

    if event:

        when = parse_date(event.group(2))
        time = parse_time(event.group(3)) if event.group(3) else ""

        if not when or time is None:
            return None

        return {
            "kind": "event",
            "title": event.group(1).strip(),
            "date": when.isoformat(),
            "time": time,
        }

    return None


async def on_type(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    kind = query.data.split(":")[1]

    context.user_data["draft"]["kind"] = kind

    await query.edit_message_text(
        "📅 Evento" if kind == "event" else "🏷️ Promozione"
    )

    await query.message.reply_text("Titolo? (es. Halloween, Serata Ribs…)")

    return TITLE


async def on_title(update: Update, context: ContextTypes.DEFAULT_TYPE):

    draft = context.user_data["draft"]
    draft["title"] = update.message.text.strip()

    if draft["kind"] == "event":
        await update.message.reply_text("Data? (es. 31/10, oppure oggi / domani)")
        return DATE

    await update.message.reply_text(
        "Fino a quando resta visibile? Scrivi una data (es. 30/11) "
        "oppure «no» se la togli tu a mano."
    )

    return UNTIL


async def on_date(update: Update, context: ContextTypes.DEFAULT_TYPE):

    when = parse_date(update.message.text)

    if not when:
        await update.message.reply_text("Non ho capito la data. Scrivila così: 31/10")
        return DATE

    if when < today():
        await update.message.reply_text("Questa data è già passata. Riprova:")
        return DATE

    context.user_data["draft"]["date"] = when.isoformat()

    await update.message.reply_text("Ora di inizio? (es. 19:30, oppure «no»)")

    return TIME


async def on_time(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text.strip()

    time = "" if normalize(text) in ("no", "-", "nessuna") else parse_time(text)

    if time is None:
        await update.message.reply_text("Non ho capito l'ora. Scrivila così: 19:30")
        return TIME

    context.user_data["draft"]["time"] = time

    return await finish_draft(update.message, context)


async def on_until(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text.strip()

    if normalize(text) in ("no", "-", "nessuna"):
        until = ""
    else:
        parsed = parse_date(text)

        if not parsed:
            await update.message.reply_text("Non ho capito. Scrivi una data (30/11) oppure «no»:")
            return UNTIL

        until = parsed.isoformat()

    context.user_data["draft"]["until"] = until

    return await finish_draft(update.message, context)


async def finish_draft(message, context):

    draft = context.user_data.pop("draft")

    stamp = draft.get("date", today().isoformat()).replace("-", "")

    stem = f"{stamp}_{slugify(draft['title'])}"

    # Nome libero, senza sovrascrivere immagini esistenti
    existing = {p.stem for p in (SITE / "images" / "events").glob("*")}
    base, n = stem, 2
    while stem in existing:
        stem, n = f"{base}-{n}", n + 1

    draft["stem"] = stem

    if draft["kind"] == "event":

        summary = (
            f"📅 <b>{html.escape(draft['title'])}</b>\n"
            f"{format_date(draft['date'])}"
            + (f" · {draft['time']}" if draft["time"] else "")
        )

        earlier = [
            e for e in read_json("events.json")
            if e.get("type") != "promo" and e.get("date", "") >= today().isoformat()
            and e["date"] < draft["date"]
        ]

        if earlier:
            summary += (
                f"\n\nℹ️ Sul sito si vede un evento alla volta: prima c'è "
                f"«{html.escape(earlier[0]['title'])}», questo comparirà dopo."
            )

    else:

        summary = f"🏷️ <b>{html.escape(draft['title'])}</b>\n" + (
            f"visibile fino al {format_date(draft['until'])}"
            if draft["until"] else "visibile finché non la togli"
        )

    await propose(message, context, draft, summary)

    return ConversationHandler.END


async def on_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data.pop("draft", None)

    await update.message.reply_text("Ok, lasciamo stare questa locandina.")

    return ConversationHandler.END


# =========================================================
# ELENCHI E RIMOZIONE
# =========================================================

async def list_entries(update, context, promos):

    await in_repo(sync)

    entries = [
        e for e in read_json("events.json")
        if (e.get("type") == "promo") == promos
    ]

    if not promos:
        entries.sort(key=lambda e: e["date"], reverse=True)

    if not entries:
        return await update.message.reply_text(
            "Nessuna promozione attiva." if promos else "Nessun evento."
        )

    buttons = []

    for entry in entries[:20]:

        label = entry["title"]

        if not promos:
            label += f" · {format_date(entry['date'])}"
        elif entry.get("until"):
            label += f" · fino {format_date(entry['until'])}"

        buttons.append([InlineKeyboardButton(
            f"🗑 {label}",
            callback_data=f"remove:{Path(entry['image']).name}",
        )])

    await update.message.reply_text(
        ("Promozioni" if promos else "Eventi") + " sul sito. Tocca per togliere:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await list_entries(update, context, promos=False)


async def cmd_promos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await list_entries(update, context, promos=True)


async def on_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    if query.from_user.id not in ALLOWED:
        return await query.answer()

    await query.answer()

    name = query.data.split(":", 1)[1]

    entry = next(
        (e for e in read_json("events.json") if Path(e["image"]).name == name),
        None,
    )

    if not entry:
        return await query.message.reply_text("Non lo trovo più, forse è già stato tolto.")

    change = {"kind": "remove", "image": entry["image"]}

    context.user_data["pending"] = change

    await query.message.reply_text(
        f"Tolgo «{entry['title']}» dal sito?",
        reply_markup=CONFIRM_BUTTONS,
    )


# =========================================================
# MENU
# =========================================================

def menu_items():

    for section in read_json("menu.json")["sections"]:
        for category in section["categories"]:
            for item in category["items"]:
                yield category, item


def search_items(query):

    query = normalize(query)

    items = list(menu_items())

    exact = [(c, i) for c, i in items if normalize(i["name"]) == query]
    if exact:
        return exact

    contained = [(c, i) for c, i in items if query in normalize(i["name"])]
    if contained:
        return contained

    names = [normalize(i["name"]) for _, i in items]
    close = difflib.get_close_matches(query, names, n=4, cutoff=0.6)

    return [(c, i) for c, i in items if normalize(i["name"]) in close]


async def menu_command(update, context, action):

    tokens = context.args[:]

    prices = parse_prices(tokens) if action == "price" else []

    query = " ".join(tokens)

    if not query or (action == "price" and not prices):
        examples = {
            "price": "/prezzo spritz 6,50",
            "hide": "/nascondi picanha",
            "show": "/mostra picanha",
        }
        return await update.message.reply_text(f"Esempio: {examples[action]}")

    await in_repo(sync)

    matches = search_items(query)

    if not matches:
        return await update.message.reply_text(
            f"Non trovo «{query}» nel menu. Controlla come è scritto su /menu."
        )

    if len(matches) > 1:

        context.user_data["menu_action"] = (action, prices)

        buttons = [
            [InlineKeyboardButton(
                item["name"],
                callback_data=f"pick:{category['id']}:{index}",
            )]
            for index, (category, item) in enumerate(matches[:6])
        ]

        context.user_data["menu_matches"] = [
            (c["id"], i["name"]) for c, i in matches[:6]
        ]

        return await update.message.reply_text(
            "Quale intendi?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    category, item = matches[0]

    await propose_menu_change(update.message, context, action, prices, category, item)


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    if query.from_user.id not in ALLOWED:
        return await query.answer()

    await query.answer()

    index = int(query.data.split(":")[2])

    action, prices = context.user_data.pop("menu_action", (None, None))
    matches = context.user_data.pop("menu_matches", [])

    if action is None or index >= len(matches):
        return await query.message.reply_text("Scelta scaduta, ripeti il comando.")

    category_id, name = matches[index]

    category, item = next(
        (c, i) for c, i in menu_items()
        if c["id"] == category_id and i["name"] == name
    )

    await query.edit_message_reply_markup(reply_markup=None)

    await propose_menu_change(query.message, context, action, prices, category, item)


async def propose_menu_change(message, context, action, prices, category, item):

    name = item["name"]
    label = html.escape(name)

    change = {"kind": "menu", "category": category["id"], "name": name}

    if action == "hide":
        change["set"] = {"available": False}
        change["summary"] = f"nascosto {name}"
        summary = f"🙈 Nascondo <b>{label}</b> dal menu."

    elif action == "show":
        change["set"] = {"available": None}
        change["summary"] = f"di nuovo visibile {name}"
        summary = f"👀 Rimetto <b>{label}</b> nel menu."

    else:
        columns = category.get("columns")

        if columns:

            if len(prices) != len(columns):
                return await message.reply_text(
                    f"Per {name} servono {len(columns)} prezzi "
                    f"({' e '.join(columns)}). Usa «-» se non c'è, es.:\n"
                    f"/prezzo {name.lower()} 4 7"
                )

            old = " / ".join(format_price(p) for p in item.get("prices", []))
            new = " / ".join(format_price(p) for p in prices)

            change["set"] = {"prices": prices}

        else:

            if len(prices) != 1:
                return await message.reply_text(f"Per {name} serve un solo prezzo.")

            old = format_price(item.get("price"))
            new = format_price(prices[0])

            change["set"] = {"price": prices[0]}

        change["summary"] = f"{name} {new}"
        summary = f"💶 <b>{label}</b>\n{old} → <b>{new}</b>"

    await propose(message, context, change, summary)


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await menu_command(update, context, "price")


async def cmd_hide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await menu_command(update, context, "hide")


async def cmd_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await menu_command(update, context, "show")


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📖 https://memphisristopub.it/menu.html")


# =========================================================
# AVVISO
# =========================================================

async def cmd_notice(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = " ".join(context.args).strip()

    if not text:

        await in_repo(sync)

        current = read_json("notice.json").get("text", "")

        return await update.message.reply_text(
            f"Avviso attuale: «{current}»\nPer toglierlo: /avviso off"
            if current else
            "Nessun avviso. Esempio:\n/avviso Chiuso per ferie dal 10 al 20 agosto"
        )

    if normalize(text) in ("off", "no", "togli"):
        return await propose(
            update.message, context,
            {"kind": "notice", "text": ""},
            "📢 Tolgo l'avviso dal sito.",
        )

    await propose(
        update.message, context,
        {"kind": "notice", "text": text},
        f"📢 Avviso in fondo al sito:\n«{html.escape(text)}»",
    )


# =========================================================
# ANNULLA E STATO
# =========================================================

def last_bot_commit():

    sync()

    author, subject, commit = git("log", "-1", "--format=%an%x09%s%x09%h").split("\t")

    return (subject, commit) if author == "Bot Memphis" else None


def revert_last():

    sync()

    subject = git("log", "-1", "--format=%s")

    git("revert", "--no-edit", "HEAD")
    git("commit", "--quiet", "--amend", "-m", f"Annullato: {subject}")
    git("push", "--quiet", "origin", "main")

    return git("rev-parse", "--short", "HEAD")


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    last = await in_repo(last_bot_commit)

    if not last:
        return await update.message.reply_text(
            "L'ultima modifica al sito non è del bot: non la annullo da qui. "
            "Chiedi a Denis."
        )

    await update.message.reply_text(
        f"Annullo l'ultima modifica?\n«{last[0]}» ({last[1]})",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ Sì, annulla", callback_data="undo:yes"),
            InlineKeyboardButton("No", callback_data="undo:no"),
        ]]),
    )


async def on_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    if query.from_user.id not in ALLOWED:
        return await query.answer()

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)

    if query.data == "undo:no":
        return await query.message.reply_text("Ok, lascio tutto com'è.")

    try:
        commit = await in_repo(revert_last)
    except Exception as error:
        log.exception("Annulla fallito")
        return await query.message.reply_text(f"⚠️ Non riuscito: {error}")

    await query.message.reply_text(
        f"↩️ Annullato ({commit}). Il sito torna com'era tra 1-2 minuti."
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await in_repo(sync)

    lines = git("log", "-8", "--format=%cd · %an · %s", "--date=format:%d/%m %H:%M")

    await update.message.reply_text(f"Ultime modifiche al sito:\n\n{lines}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="HTML")


async def on_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Non ho capito. Scrivi /aiuto per vedere cosa posso fare.")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):

    log.exception("Errore", exc_info=context.error)

    if isinstance(update, Update) and update.effective_chat:
        await context.bot.send_message(
            update.effective_chat.id,
            f"⚠️ Qualcosa è andato storto: {context.error}",
        )


# =========================================================
# AVVIO
# =========================================================

def main():

    if not ENV.get("TELEGRAM_TOKEN") or not ALLOWED:
        raise SystemExit("Configura TELEGRAM_TOKEN e ALLOWED_USER_IDS in .env")

    app = Application.builder().token(ENV["TELEGRAM_TOKEN"]).build()

    text = AUTH & filters.TEXT & ~filters.COMMAND

    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(AUTH & (filters.PHOTO | filters.Document.IMAGE), on_photo)],
        states={
            TYPE: [CallbackQueryHandler(on_type, pattern=r"^type:")],
            TITLE: [MessageHandler(text, on_title)],
            DATE: [MessageHandler(text, on_date)],
            TIME: [MessageHandler(text, on_time)],
            UNTIL: [MessageHandler(text, on_until)],
        },
        fallbacks=[CommandHandler("stop", on_stop, filters=AUTH)],
        conversation_timeout=60 * 30,
    ))

    commands = {
        ("start", "aiuto", "help"): cmd_help,
        ("eventi",): cmd_events,
        ("promozioni", "promo"): cmd_promos,
        ("prezzo",): cmd_price,
        ("nascondi",): cmd_hide,
        ("mostra",): cmd_show,
        ("menu",): cmd_menu,
        ("avviso",): cmd_notice,
        ("annulla",): cmd_undo,
        ("stato",): cmd_status,
    }

    for names, handler in commands.items():
        app.add_handler(CommandHandler(list(names), handler, filters=AUTH))

    app.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^(publish|discard)$"))
    app.add_handler(CallbackQueryHandler(on_remove, pattern=r"^remove:"))
    app.add_handler(CallbackQueryHandler(on_pick, pattern=r"^pick:"))
    app.add_handler(CallbackQueryHandler(on_undo, pattern=r"^undo:"))

    app.add_handler(MessageHandler(AUTH, on_other))

    app.add_error_handler(on_error)

    log.info("Bot avviato, anteprime su %s", PREVIEW_URL)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
