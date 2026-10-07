#!/usr/bin/env python3
"""
Bot Telegram per aggiornare memphisristopub.it senza toccare il codice.

Gira su una copia dedicata del repository (~/memphis-bot/site) e per ogni
modifica: aggiorna i file, mostra un'anteprima, e dopo la conferma fa
commit + push. GitHub Pages pubblica in 1-2 minuti.

Si usa con i pulsanti: tastiera fissa in basso (Locandina, Eventi,
Promozioni, Menu, Avviso, Altro) e menu a pulsanti dentro i messaggi.
I comandi scritti (/prezzo, /avviso...) restano come scorciatoie.

Configurazione in ~/memphis-bot/.env:

    TELEGRAM_TOKEN=...          token da @BotFather
    ALLOWED_USER_IDS=123,456    ID Telegram autorizzati

La parte su file, git e anteprime è in core.py.
"""

import asyncio
import calendar
import difflib
import html
import json
import logging
from datetime import date, timedelta
from pathlib import Path

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import core
from core import (
    SITE,
    UPLOADS,
    format_date,
    format_price,
    item_price,
    normalize,
    parse_date,
    parse_prices,
    parse_time,
    read_json,
    short_date,
    slugify,
    today,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("memphis-bot")


ENV = core.load_env()

ALLOWED = {
    int(value)
    for value in ENV.get("ALLOWED_USER_IDS", "").split(",")
    if value.strip()
}

AUTH = filters.User(user_id=ALLOWED)

SITE_URL = "https://memphisristopub.it"

# Versione del codice in esecuzione (il servizio fa git pull prima dell'avvio)
RUNNING_VERSION = core.head()

# Ogni quanto controllare se il sito è stato modificato da fuori
SITE_CHECK_SECONDS = 180


# =========================================================
# TASTIERE
# =========================================================

B_POSTER = "📷 Nuova locandina"
B_EVENTS = "📅 Eventi"
B_PROMOS = "🏷️ Promozioni"
B_MENU = "🍔 Menu"
B_NOTICE = "📢 Avviso"
B_MORE = "⚙️ Altro"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[B_POSTER, B_EVENTS], [B_PROMOS, B_MENU], [B_NOTICE, B_MORE]],
    resize_keyboard=True,
    is_persistent=True,
)

CANCEL = ("✖️ Annulla", "cancel")


def keyboard(*rows):
    """Tastiera nel messaggio: righe di (testo, callback_data | URL)."""

    def button(label, data):
        if data.startswith("http"):
            return InlineKeyboardButton(label, url=data)
        return InlineKeyboardButton(label, callback_data=data)

    return InlineKeyboardMarkup([
        [button(label, data) for label, data in row]
        for row in rows if row
    ])


def pairs(buttons):
    """Pulsanti a due per riga."""
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def esc(text):
    return html.escape(str(text))


async def respond(update, text, markup=None):
    """
    Dai pulsanti di navigazione si aggiorna lo stesso messaggio,
    così la chat non si riempie; altrimenti si manda un messaggio nuovo.
    """

    query = update.callback_query

    if query and query.message and query.message.text is not None:
        try:
            return await query.edit_message_text(
                text, reply_markup=markup, parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except BadRequest as error:
            if "not modified" in str(error):
                return
            raise

    return await update.effective_message.reply_text(
        text, reply_markup=markup, parse_mode="HTML",
        disable_web_page_preview=True,
    )


# Una modifica alla volta sulla copia del repository
LOCK = asyncio.Lock()


async def in_repo(func, *args):
    async with LOCK:
        return await asyncio.to_thread(func, *args)


# =========================================================
# CONFERMA E PUBBLICAZIONE
# =========================================================



async def propose(update, context, change, summary, back=None):
    """Anteprima + riepilogo, in attesa di conferma."""

    message = update.effective_message

    change["back"] = back
    context.user_data.pop("awaiting", None)

    # Ogni anteprima ha il suo id: i pulsanti di una vecchia
    # anteprima non possono pubblicare la modifica di un'altra
    pending = context.user_data.setdefault("pending", {})
    context.user_data["next_id"] = pid = context.user_data.get("next_id", 0) + 1
    pending[str(pid)] = change

    for old in sorted(pending, key=int)[:-5]:
        del pending[old]

    waiting = await message.reply_text("⏳ Preparo l'anteprima…")

    try:
        image = await in_repo(core.preview, change)
    except Exception as error:
        log.exception("Anteprima fallita")
        pending.pop(str(pid), None)
        return await waiting.edit_text(f"⚠️ Non riesco a preparare l'anteprima: {error}")

    await waiting.delete()

    await message.reply_photo(
        photo=image,
        caption=f"{summary}\n\nPubblico sul sito?",
        reply_markup=keyboard([("✅ Pubblica", f"publish:{pid}"), ("❌ Annulla", f"discard:{pid}")]),
        parse_mode="HTML",
    )


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, action, pid):

    query = update.callback_query

    await query.edit_message_reply_markup(reply_markup=None)

    change = context.user_data.get("pending", {}).pop(pid, None)

    if action == "discard" or change is None:
        text = "Annullato, non ho pubblicato nulla." if change else "Questa anteprima è scaduta."
        return await query.message.reply_text(text)

    status = await query.message.reply_text("⏳ Pubblico…")

    try:
        commit, paths = await in_repo(core.publish, change)
    except Exception as error:
        log.exception("Pubblicazione fallita")
        return await status.edit_text(f"⚠️ Pubblicazione non riuscita: {error}")

    back = change.get("back")

    await status.edit_text(
        "✅ Pubblicato. Sul sito sarà visibile tra 1-2 minuti: ti avviso io.",
        reply_markup=keyboard([back]) if back else None,
    )

    watched = next(p for p in paths if p.endswith(".json"))
    expected = json.loads((SITE / watched).read_text())

    context.application.create_task(
        wait_until_live(context, query.message.chat_id, watched, expected)
    )


async def wait_until_live(context, chat_id, path, expected):
    """Controlla il sito finché il file pubblicato non è aggiornato."""

    for _ in range(36):

        await asyncio.sleep(10)

        if await asyncio.to_thread(core.fetch_live, path) == expected:
            return await context.bot.send_message(
                chat_id, f"🌐 È online: {SITE_URL}",
                disable_web_page_preview=True,
            )

    await context.bot.send_message(
        chat_id,
        "⚠️ Dopo 6 minuti il sito non risulta ancora aggiornato. "
        "Controlla tra poco; se non cambia avvisa Denis.",
    )


# =========================================================
# DOMANDE (risposta scritta o pulsante rapido)
# =========================================================

async def ask(update, context, step, ctx, prompt, quick=()):
    """
    Fa una domanda e aspetta la risposta. Si può scrivere
    oppure usare i pulsanti rapidi (quick: righe di (testo, valore)).
    """

    context.user_data["awaiting"] = {"step": step, "ctx": ctx}

    rows = [[(label, f"q:{value}") for label, value in row] for row in quick]

    await update.effective_message.reply_text(
        prompt,
        reply_markup=keyboard(*rows, [CANCEL]),
        parse_mode="HTML",
    )


async def ask_title(update, context, ctx, current=None):

    prompt = "Scrivi il <b>titolo</b> (es. Halloween, Serata Ribs…)"

    if current:
        prompt += f"\nAttuale: «{esc(current)}»"

    await ask(update, context, "title", ctx, prompt)


async def ask_date(update, context, ctx):

    start = today()
    days = [start, start + timedelta(days=1)]

    # Prossimi venerdì, sabato e domenica
    for offset in range(2, 9):
        day = start + timedelta(days=offset)
        if day.weekday() >= 4 and len(days) < 6:
            days.append(day)

    labels = ["Oggi", "Domani"] + [short_date(d) for d in days[2:]]

    quick = pairs([(label, day.isoformat()) for label, day in zip(labels, days)])

    await ask(update, context, "date", ctx,
              "📅 <b>Data</b>? Scegli o scrivi (es. 31/10)", quick)


async def ask_time(update, context, ctx):

    quick = [
        [("19:00", "19:00"), ("19:30", "19:30"), ("20:00", "20:00")],
        [("20:30", "20:30"), ("21:00", "21:00"), ("22:00", "22:00")],
        [("Nessun orario", "none")],
    ]

    await ask(update, context, "time", ctx,
              "🕑 <b>Ora di inizio</b>? Scegli o scrivi (es. 19:30)", quick)


async def ask_until(update, context, ctx):

    start = today()
    month_end = start.replace(day=calendar.monthrange(start.year, start.month)[1])

    quick = [
        [("Nessuna scadenza", "none")],
        [("Tra 1 settimana", (start + timedelta(days=7)).isoformat()),
         (f"Fine mese ({month_end:%d/%m})", month_end.isoformat())],
    ]

    await ask(update, context, "until", ctx,
              "⏳ <b>Fino a quando</b> resta visibile? Scegli o scrivi una data", quick)


def read_choice(step, raw, typed):
    """Converte la risposta in valore. Restituisce (valore, errore)."""

    text = raw.strip()
    empty = normalize(text) in ("no", "nessuna", "nessuno", "-", "none")

    if step in ("date", "until"):

        if step == "until" and empty:
            return "", None

        day = parse_date(text) if typed else date.fromisoformat(text)

        if not day:
            return None, "Non ho capito la data. Scrivila così: 31/10"

        if day < today():
            return None, "Questa data è già passata."

        return day.isoformat(), None

    if step == "time":

        if empty:
            return "", None

        time = parse_time(text)

        return (time, None) if time else (None, "Non ho capito l'ora. Scrivila così: 19:30")

    if step in ("title", "name", "add_name"):

        if not text:
            return None, "Il testo è vuoto."

        return text[:60], None

    if step in ("desc", "add_desc"):
        return (None if empty else text[:200]), None

    if step == "notice":
        return (text[:200], None) if text else (None, "Il testo è vuoto.")

    return text, None


async def handle_answer(update, context, raw, typed):

    awaiting = context.user_data.get("awaiting")

    if not awaiting:
        return await update.effective_message.reply_text(
            "Questa domanda è scaduta. Ricomincia dai pulsanti qui sotto 👇",
            reply_markup=MAIN_KEYBOARD,
        )

    step, ctx = awaiting["step"], awaiting["ctx"]

    # Prezzi: gestione a parte (uno o più numeri)
    if step in ("price", "add_price"):
        return await handle_price(update, context, step, ctx, raw)

    value, error = read_choice(step, raw, typed)

    if error:
        return await update.effective_message.reply_text(
            f"{error} Riprova, oppure premi ✖️ Annulla."
        )

    mode = ctx.get("mode")

    # --- Nuova locandina ---
    if mode == "draft":

        draft = context.user_data.get("draft")

        if not draft:
            return await update.effective_message.reply_text("Locandina scaduta, rimandala.")

        draft[step] = value

        if step == "title":
            if draft["kind"] == "event":
                return await ask_date(update, context, ctx)
            return await ask_until(update, context, ctx)

        if step == "date":
            return await ask_time(update, context, ctx)

        return await finish_draft(update, context)

    # --- Modifica evento / promozione ---
    if mode == "entry":
        return await propose_entry_edit(update, context, ctx["image"], step, value)

    # --- Menu ---
    if mode == "item":
        return await propose_item_text(update, context, ctx, step, value)

    if mode == "add":

        ctx["item"]["name" if step == "add_name" else "description"] = value

        if step == "add_name":
            if value in {i["name"] for i in get_items(ctx["category"])}:
                return await update.effective_message.reply_text(
                    f"«{value}» esiste già in questa categoria. Scrivi un altro nome:"
                )
            return await ask(
                update, context, "add_desc", ctx,
                "📝 <b>Descrizione</b>? (es. ingredienti) Scrivila oppure:",
                [[("Nessuna descrizione", "none")]],
            )

        if value is None:
            ctx["item"].pop("description")

        return await ask_price(update, context, "add_price", ctx)

    # --- Avviso ---
    if mode == "notice":
        return await propose(
            update, context,
            {"kind": "notice", "text": value},
            f"📢 Avviso in fondo al sito:\n«{esc(value)}»",
        )


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data.pop("awaiting", None)
    context.user_data.pop("draft", None)

    await respond(update, "Ok, annullato.")


# =========================================================
# NUOVA LOCANDINA
# =========================================================

async def poster_prompt(update, context):

    context.user_data.pop("awaiting", None)

    await respond(
        update,
        "📷 Mandami la locandina.\n\n"
        "💡 Per la qualità migliore mandala come <b>file</b> "
        "(📎 → File), non come foto.",
    )


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

    context.user_data.pop("awaiting", None)
    context.user_data["draft"] = draft = {"upload": str(upload)}

    caption = (message.caption or "").strip()

    if caption:
        quick = core.parse_caption(caption)
        if quick:
            draft.update(quick)
            return await finish_draft(update, context)

    await message.reply_text(
        "Ricevuta! È un evento o una promozione?",
        reply_markup=keyboard(
            [("📅 Evento", "type:event"), ("🏷️ Promozione", "type:promo")],
            [CANCEL],
        ),
    )


async def on_type(update, context, kind):

    draft = context.user_data.get("draft")

    if not draft:
        return await respond(update, "Locandina scaduta, rimandala.")

    draft["kind"] = kind

    await respond(update, "📅 Evento" if kind == "event" else "🏷️ Promozione")

    await ask_title(update, context, {"mode": "draft"})


async def finish_draft(update, context):

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
            f"📅 <b>{esc(draft['title'])}</b>\n"
            f"{format_date(draft['date'])}"
            + (f" · {draft['time']}" if draft["time"] else "")
        )

        earlier = [
            e for e in read_json("events.json")
            if e.get("type") != "promo"
            and today().isoformat() <= e.get("date", "") < draft["date"]
        ]

        if earlier:
            summary += (
                f"\n\nℹ️ Sul sito si vede un evento alla volta: prima c'è "
                f"«{esc(earlier[0]['title'])}», questo comparirà dopo."
            )

        back = ("⬅️ Eventi", "list:events")

    else:

        summary = f"🏷️ <b>{esc(draft['title'])}</b>\n" + (
            f"visibile fino al {format_date(draft['until'])}"
            if draft["until"] else "visibile finché non la togli"
        )

        back = ("⬅️ Promozioni", "list:promos")

    await propose(update, context, draft, summary, back)


# =========================================================
# EVENTI E PROMOZIONI
# =========================================================

async def entries_screen(update, context, promos):

    context.user_data.pop("awaiting", None)

    await in_repo(core.sync)

    entries = [
        e for e in read_json("events.json")
        if (e.get("type") == "promo") == promos
    ]

    buttons = []

    if promos:

        for e in entries:
            label = e["title"] + (f" · fino {e['until'][8:10]}/{e['until'][5:7]}" if e.get("until") else "")
            buttons.append([(f"🏷️ {label}", f"en:{Path(e['image']).name}")])

        title = "🏷️ <b>Promozioni</b> sul sito"
        add = ("➕ Nuova promozione", "newposter")

    else:

        now = today().isoformat()

        upcoming = sorted((e for e in entries if e["date"] >= now), key=lambda e: e["date"])
        past = sorted((e for e in entries if e["date"] < now), key=lambda e: e["date"], reverse=True)

        for e in upcoming:
            buttons.append([(f"📅 {e['title']} · {format_date(e['date'])[:5]}", f"en:{Path(e['image']).name}")])

        for e in past[:5]:
            buttons.append([(f"🕘 {e['title']} · {format_date(e['date'])[:5]}", f"en:{Path(e['image']).name}")])

        title = "📅 <b>Eventi</b> (🕘 = già passati)"
        add = ("➕ Nuovo evento", "newposter")

    if not buttons:
        title += "\n\nAl momento non ce ne sono."

    await respond(update, f"{title}\nTocca per modificare o togliere.", keyboard(*buttons, [add]))


async def entry_screen(update, context, name):

    entry = next(
        (e for e in read_json("events.json") if Path(e["image"]).name == name),
        None,
    )

    if not entry:
        return await respond(update, "Non lo trovo più: forse è già stato tolto.")

    promo = entry.get("type") == "promo"

    lines = [f"{'🏷️' if promo else '📅'} <b>{esc(entry['title'])}</b>"]

    if promo:
        lines.append(
            f"⏳ Fino al {format_date(entry['until'])}" if entry.get("until")
            else "⏳ Nessuna scadenza"
        )
        rows = [
            [("✏️ Titolo", f"ea:title:{name}"), ("⏳ Scadenza", f"ea:until:{name}")],
            [("🗑 Togli dal sito", f"ea:remove:{name}")],
            [("🖼 Vedi locandina", f"{SITE_URL}/{entry['image']}")],
            [("⬅️ Promozioni", "list:promos")],
        ]
    else:
        lines.append(f"📅 {format_date(entry['date'])}" + (f" · 🕑 {entry['time']}" if entry.get("time") else ""))
        rows = [
            [("✏️ Titolo", f"ea:title:{name}"), ("📅 Data", f"ea:date:{name}")],
            [("🕑 Ora", f"ea:time:{name}"), ("🗑 Togli dal sito", f"ea:remove:{name}")],
            [("🖼 Vedi locandina", f"{SITE_URL}/{entry['image']}")],
            [("⬅️ Eventi", "list:events")],
        ]

    await respond(update, "\n".join(lines), keyboard(*rows))


async def entry_action(update, context, action, name):

    entry = next(
        (e for e in read_json("events.json") if Path(e["image"]).name == name),
        None,
    )

    if not entry:
        return await respond(update, "Non lo trovo più: forse è già stato tolto.")

    promo = entry.get("type") == "promo"
    back = ("⬅️ Promozioni", "list:promos") if promo else ("⬅️ Eventi", "list:events")

    if action == "remove":
        return await propose(
            update, context,
            {"kind": "remove", "image": entry["image"]},
            f"🗑 Tolgo «{esc(entry['title'])}» dal sito.",
            back,
        )

    ctx = {"mode": "entry", "image": entry["image"]}

    if action == "title":
        return await ask_title(update, context, ctx, entry["title"])
    if action == "date":
        return await ask_date(update, context, ctx)
    if action == "time":
        return await ask_time(update, context, ctx)
    if action == "until":
        return await ask_until(update, context, ctx)


async def propose_entry_edit(update, context, image, field, value):

    entry = core.get_entry(image)

    if not entry:
        return await update.effective_message.reply_text("Non lo trovo più: forse è già stato tolto.")

    promo = entry.get("type") == "promo"

    if field == "title":
        label = f"✏️ Titolo: «{esc(value)}»"
    elif field == "date":
        label = f"📅 Data: {format_date(value)}"
    elif field == "time":
        label = f"🕑 Ora: {value or 'nessun orario'}"
    else:
        label = f"⏳ Fino al {format_date(value)}" if value else "⏳ Nessuna scadenza"

    await propose(
        update, context,
        {"kind": "entry_edit", "image": image, "set": {field: value}},
        f"<b>{esc(entry['title'])}</b>\n{label}",
        ("⬅️ Promozioni", "list:promos") if promo else ("⬅️ Eventi", "list:events"),
    )


# =========================================================
# MENU
# =========================================================

def get_items(category_id):
    return next(c for c in core.categories() if c["id"] == category_id)["items"]


def item_label(item):

    flags = ("🙈 " if item.get("available") is False else "") + ("⭐ " if item.get("featured") else "")

    label = f"{flags}{item['name']} · {item_price(item)}"

    return label if len(label) <= 60 else label[:57] + "…"


async def menu_screen(update, context):

    context.user_data.pop("awaiting", None)

    await in_repo(core.sync)

    buttons = [
        (f"{c['title']} ({len(c['items'])})", f"cat:{c['id']}")
        for c in core.categories()
    ]

    await respond(
        update,
        "🍔 <b>Menu</b>\nScegli una categoria:",
        keyboard(*pairs(buttons), [("📖 Vedi il menu sul sito", f"{SITE_URL}/menu.html")]),
    )


async def category_screen(update, context, category_id):

    context.user_data.pop("awaiting", None)

    category = next((c for c in core.categories() if c["id"] == category_id), None)

    if not category:
        return await menu_screen(update, context)

    rows = [
        [(item_label(item), f"it:{category_id}:{index}")]
        for index, item in enumerate(category["items"])
    ]

    await respond(
        update,
        f"🍔 <b>{esc(category['title'])}</b>\n"
        "Tocca una voce per modificarla.\n⭐ = in homepage · 🙈 = nascosta",
        keyboard(
            *rows,
            [("➕ Aggiungi voce", f"add:{category_id}")],
            [("⬅️ Categorie", "menu")],
        ),
    )


def load_item(category_id, index):

    category = next((c for c in core.categories() if c["id"] == category_id), None)

    if not category or index >= len(category["items"]):
        return None, None

    return category, category["items"][index]


async def item_screen(update, context, category_id, index):

    context.user_data.pop("awaiting", None)

    category, item = load_item(category_id, index)

    if not item:
        return await category_screen(update, context, category_id)

    if "prices" in item:
        price = " · ".join(
            f"{label} {format_price(p)}"
            for label, p in zip(category.get("columns", []), item["prices"])
        )
    else:
        price = format_price(item.get("price"))

    hidden = item.get("available") is False
    featured = bool(item.get("featured"))

    text = "\n".join([
        f"<b>{esc(item['name'])}</b>",
        f"<i>{esc(category['title'])}</i>",
        "",
        f"💶 {price}",
        f"📝 {esc(item.get('description') or 'nessuna descrizione')}",
        f"⭐ In homepage: {'sì' if featured else 'no'}",
        "🙈 Nascosta dal menu" if hidden else "👀 Visibile nel menu",
    ])

    ref = f"{category_id}:{index}"

    await respond(update, text, keyboard(
        [("💶 Prezzo", f"ia:price:{ref}"), ("✏️ Nome", f"ia:name:{ref}")],
        [("📝 Descrizione", f"ia:desc:{ref}"),
         ("⭐ Togli da homepage" if featured else "⭐ Metti in homepage", f"ia:feat:{ref}")],
        [("👀 Mostra" if hidden else "🙈 Nascondi", f"ia:vis:{ref}"), ("🗑 Elimina", f"ia:del:{ref}")],
        [(f"⬅️ {category['title']}", f"cat:{category_id}")],
    ))


async def item_action(update, context, action, category_id, index):

    category, item = load_item(category_id, index)

    if not item:
        return await category_screen(update, context, category_id)

    name = item["name"]
    back = (f"⬅️ {category['title']}", f"cat:{category_id}")
    ctx = {"mode": "item", "category": category_id, "name": name,
           "columns": category.get("columns")}

    if action == "price":
        return await ask_price(update, context, "price", ctx, item)

    if action == "name":
        return await ask(update, context, "name", ctx,
                         f"✏️ Nuovo <b>nome</b> per «{esc(name)}»:")

    if action == "desc":
        return await ask(
            update, context, "desc", ctx,
            f"📝 Nuova <b>descrizione</b> per «{esc(name)}»"
            f"\nAttuale: {esc(item.get('description') or 'nessuna')}",
            [[("Nessuna descrizione", "none")]],
        )

    if action == "feat":

        if item.get("featured"):
            return await propose_menu_set(
                update, context, category_id, name, {"featured": None},
                f"tolto {name} dalla homepage",
                f"⭐ Tolgo <b>{esc(name)}</b> dalla homepage.", back,
            )

        # In homepage le due colonne (Cucina, Bevande) stanno meglio pari
        menu = read_json("menu.json")
        counts = [
            sum(1 for c in s["categories"] for i in c["items"] if i.get("featured"))
            for s in menu["sections"]
        ]

        note = f"\n\nℹ️ In homepage ora: Cucina {counts[0]}, Bevande {counts[1]}. Tienile pari per l'allineamento."

        return await propose_menu_set(
            update, context, category_id, name, {"featured": True},
            f"{name} in homepage",
            f"⭐ Metto <b>{esc(name)}</b> in homepage.{note}", back,
        )

    if action == "vis":

        if item.get("available") is False:
            return await propose_menu_set(
                update, context, category_id, name, {"available": None},
                f"di nuovo visibile {name}",
                f"👀 Rimetto <b>{esc(name)}</b> nel menu.", back,
            )

        return await propose_menu_set(
            update, context, category_id, name, {"available": False},
            f"nascosto {name}",
            f"🙈 Nascondo <b>{esc(name)}</b> dal menu (resta salvata, la rimetti quando vuoi).", back,
        )

    if action == "del":
        return await propose(
            update, context,
            {"kind": "menu_delete", "category": category_id, "name": name},
            f"🗑 Elimino <b>{esc(name)}</b> dal menu.\n"
            "Se è solo finita per un po', meglio 🙈 Nascondi.",
            back,
        )


async def ask_price(update, context, step, ctx, item=None):

    columns = ctx.get("columns")

    if columns:
        example = " ".join(["4"] * len(columns))
        prompt = (
            f"💶 Scrivi i <b>{len(columns)} prezzi</b> ({' e '.join(columns)}), "
            f"separati da spazio. Es: <code>{example}</code>\n"
            "Usa <code>-</code> se una misura non c'è."
        )
    else:
        prompt = "💶 Scrivi il <b>prezzo</b>. Es: <code>6,50</code>"

    if item:
        prompt = f"<b>{esc(item['name'])}</b> · attuale {item_price(item)}\n\n" + prompt

    await ask(update, context, step, ctx, prompt)


async def handle_price(update, context, step, ctx, raw):

    tokens = raw.replace("€", " ").split()
    prices = core.parse_prices(tokens)

    columns = ctx.get("columns")
    expected = len(columns) if columns else 1

    if tokens or len(prices) != expected:
        problem = (
            f"Mi servono {expected} prezzi ({' e '.join(columns)}), es. 4 7."
            if columns else "Non ho capito il prezzo, es. 6,50."
        )
        return await update.effective_message.reply_text(
            f"{problem} Riprova, oppure premi ✖️ Annulla."
        )

    value = {"prices": prices} if columns else {"price": prices[0]}
    shown = " / ".join(format_price(p) for p in prices)

    if step == "add_price":

        item = {"name": ctx["item"]["name"]}
        if ctx["item"].get("description"):
            item["description"] = ctx["item"]["description"]
        item.update(value)

        category = next(c for c in core.categories() if c["id"] == ctx["category"])

        return await propose(
            update, context,
            {"kind": "menu_add", "category": ctx["category"], "item": item},
            f"➕ Aggiungo a <b>{esc(category['title'])}</b>:\n"
            f"<b>{esc(item['name'])}</b> · {shown}"
            + (f"\n{esc(item['description'])}" if item.get("description") else ""),
            (f"⬅️ {category['title']}", f"cat:{ctx['category']}"),
        )

    await propose_menu_set(
        update, context, ctx["category"], ctx["name"], value,
        f"{ctx['name']} {shown}",
        f"💶 <b>{esc(ctx['name'])}</b> → <b>{shown}</b>",
        ("⬅️ Torna al menu", f"cat:{ctx['category']}"),
    )


async def propose_item_text(update, context, ctx, step, value):

    name = ctx["name"]
    back = ("⬅️ Torna al menu", f"cat:{ctx['category']}")

    if step == "name":

        if value in {i["name"] for i in get_items(ctx["category"])}:
            return await update.effective_message.reply_text(
                f"«{value}» esiste già in questa categoria. Scrivi un altro nome:"
            )

        return await propose_menu_set(
            update, context, ctx["category"], name, {"name": value},
            f"{name} rinominato {value}",
            f"✏️ «{esc(name)}» diventa <b>{esc(value)}</b>", back,
        )

    if step == "desc":
        return await propose_menu_set(
            update, context, ctx["category"], name, {"description": value},
            f"descrizione {name}",
            f"📝 <b>{esc(name)}</b>\n{esc(value) if value else 'senza descrizione'}", back,
        )


async def propose_menu_set(update, context, category_id, name, values, commit, summary, back):

    await propose(
        update, context,
        {"kind": "menu", "category": category_id, "name": name,
         "set": values, "summary": commit},
        summary,
        back,
    )


async def add_start(update, context, category_id):

    category = next((c for c in core.categories() if c["id"] == category_id), None)

    if not category:
        return await menu_screen(update, context)

    await ask(
        update, context, "add_name",
        {"mode": "add", "category": category_id, "columns": category.get("columns"), "item": {}},
        f"➕ Nuova voce in <b>{esc(category['title'])}</b>\n\nScrivi il <b>nome</b>:",
    )


# =========================================================
# AVVISO
# =========================================================

async def notice_screen(update, context):

    context.user_data.pop("awaiting", None)

    await in_repo(core.sync)

    current = read_json("notice.json").get("text", "")

    if current:
        text = f"📢 <b>Avviso sul sito</b>\n«{esc(current)}»"
        rows = [[("✏️ Cambia testo", "notice:edit"), ("🗑 Togli avviso", "notice:off")]]
    else:
        text = (
            "📢 <b>Avviso</b>\nNessun avviso sul sito.\n\n"
            "Serve per comunicazioni veloci, es. «Chiuso per ferie dal 10 al 20 agosto»."
        )
        rows = [[("✏️ Scrivi un avviso", "notice:edit")]]

    await respond(update, text, keyboard(*rows))


async def notice_action(update, context, action):

    if action == "off":
        return await propose(
            update, context, {"kind": "notice", "text": ""},
            "📢 Tolgo l'avviso dal sito.",
        )

    await ask(
        update, context, "notice", {"mode": "notice"},
        "📢 Scrivi il <b>testo dell'avviso</b>.\n"
        "Es: <i>Chiuso per ferie dal 10 al 20 agosto, ci vediamo il 21!</i>",
    )


# =========================================================
# ALTRO: annulla, ultime modifiche, guida
# =========================================================

HELP = """\
<b>Come si usa</b>

Usa i pulsanti in basso 👇

📷 <b>Nuova locandina</b> — manda la foto (meglio come file 📎) e rispondi alle domande. Prima di pubblicare vedi l'anteprima.
📅 <b>Eventi</b> · 🏷️ <b>Promozioni</b> — modifica titolo, data, ora, scadenza, oppure togli.
🍔 <b>Menu</b> — categoria → voce → prezzo, nome, descrizione, homepage, nascondi, elimina. ➕ per aggiungere.
📢 <b>Avviso</b> — barra in fondo al sito per comunicazioni veloci.
⚙️ <b>Altro</b> — annulla l'ultima modifica, storico.

Niente viene pubblicato senza il tuo ✅.

<b>Scorciatoie</b> per chi va di fretta:
• didascalia della foto: <code>Halloween 31/10 19:30</code> o <code>promo Ribs fino 30/11</code>
• <code>/prezzo spritz 6,50</code> · <code>/nascondi picanha</code> · <code>/mostra picanha</code>
• <code>/avviso testo</code> · /stop interrompe una domanda"""


async def more_screen(update, context):

    context.user_data.pop("awaiting", None)

    await respond(update, "⚙️ <b>Altro</b>", keyboard(
        [("↩️ Annulla l'ultima modifica", "more:undo")],
        [("🕑 Ultime modifiche", "more:log")],
        [("❓ Come si usa", "more:help")],
    ))


async def more_action(update, context, action):

    if action == "help":
        return await respond(update, HELP, keyboard([("⬅️ Altro", "more")]))

    if action == "log":
        lines = await in_repo(core.recent_changes)
        return await respond(
            update, f"🕑 <b>Ultime modifiche al sito</b>\n\n{esc(lines)}",
            keyboard([("⬅️ Altro", "more")]),
        )

    if action == "undo":

        last = await in_repo(core.last_bot_commit)

        if not last:
            return await respond(
                update,
                "L'ultima modifica al sito non è stata fatta dal bot: "
                "non la annullo da qui. Chiedi a Denis.",
                keyboard([("⬅️ Altro", "more")]),
            )

        return await respond(
            update,
            f"↩️ Annullo l'ultima modifica?\n«{esc(last[0])}»",
            keyboard([("Sì, annulla", "undo:yes"), ("No", "more")]),
        )


async def undo_confirmed(update, context):

    await respond(update, "⏳ Annullo…")

    try:
        await in_repo(core.revert_last)
    except Exception as error:
        log.exception("Annulla fallito")
        return await respond(update, f"⚠️ Non riuscito: {error}")

    await respond(update, "↩️ Annullato. Il sito torna com'era tra 1-2 minuti.")


# =========================================================
# SCORCIATOIE SCRITTE
# =========================================================

def search_items(query):

    query = normalize(query)

    items = [(c, i) for c in core.categories() for i in c["items"]]

    for test in (
        lambda i: normalize(i["name"]) == query,
        lambda i: query in normalize(i["name"]),
    ):
        found = [(c, i) for c, i in items if test(i)]
        if found:
            return found

    close = set(difflib.get_close_matches(query, [normalize(i["name"]) for _, i in items], n=4, cutoff=0.6))

    return [(c, i) for c, i in items if normalize(i["name"]) in close]


async def shortcut(update, context, action):

    tokens = context.args[:]
    prices = parse_prices(tokens) if action == "price" else []
    query = " ".join(tokens)

    if not query or (action == "price" and not prices):
        examples = {"price": "/prezzo spritz 6,50", "vis": "/nascondi picanha", "show": "/mostra picanha"}
        return await update.message.reply_text(f"Esempio: {examples[action]}")

    await in_repo(core.sync)

    matches = search_items(query)

    if not matches:
        return await update.message.reply_text(f"Non trovo «{query}» nel menu. Prova da 🍔 Menu.")

    if len(matches) > 1:
        rows = [
            [(i["name"], f"it:{c['id']}:{c['items'].index(i)}")]
            for c, i in matches[:6]
        ]
        return await update.message.reply_text("Quale intendi?", reply_markup=keyboard(*rows))

    category, item = matches[0]
    index = category["items"].index(item)

    if action == "price":
        context.user_data["awaiting"] = {
            "step": "price",
            "ctx": {"mode": "item", "category": category["id"], "name": item["name"],
                    "columns": category.get("columns")},
        }
        return await handle_price(
            update, context, "price", context.user_data["awaiting"]["ctx"],
            " ".join(str(p) if p is not None else "-" for p in prices),
        )

    hidden = item.get("available") is False

    if (action == "show") != hidden:
        state = "già nascosta" if hidden else "già visibile"
        return await update.message.reply_text(f"{item['name']} è {state}.")

    await item_action(update, context, "vis", category["id"], index)


async def cmd_price(update, context):
    await shortcut(update, context, "price")


async def cmd_hide(update, context):
    await shortcut(update, context, "vis")


async def cmd_show(update, context):
    await shortcut(update, context, "show")


async def cmd_notice(update, context):

    text = " ".join(context.args).strip()

    if not text:
        return await notice_screen(update, context)

    if normalize(text) in ("off", "no", "togli"):
        return await notice_action(update, context, "off")

    context.user_data["awaiting"] = {"step": "notice", "ctx": {"mode": "notice"}}

    await handle_answer(update, context, text, typed=True)


# =========================================================
# SMISTAMENTO
# =========================================================

async def cmd_start(update, context):

    context.user_data.clear()

    await update.message.reply_text(
        "👋 Ciao! Da qui aggiorni il sito del MEMPHIS①.\n"
        "Usa i pulsanti qui sotto 👇",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_help(update, context):
    await update.message.reply_text(HELP, parse_mode="HTML", reply_markup=MAIN_KEYBOARD)


async def cmd_stop(update, context):
    await on_cancel(update, context)


MAIN_ACTIONS = {
    B_POSTER: poster_prompt,
    B_EVENTS: lambda u, c: entries_screen(u, c, promos=False),
    B_PROMOS: lambda u, c: entries_screen(u, c, promos=True),
    B_MENU: menu_screen,
    B_NOTICE: notice_screen,
    B_MORE: more_screen,
}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text

    # I pulsanti fissi hanno la precedenza su una domanda lasciata a metà
    if text in MAIN_ACTIONS:
        context.user_data.pop("awaiting", None)
        return await MAIN_ACTIONS[text](update, context)

    if context.user_data.get("awaiting"):
        return await handle_answer(update, context, text, typed=True)

    await update.message.reply_text(
        "Usa i pulsanti qui sotto 👇", reply_markup=MAIN_KEYBOARD,
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    if query.from_user.id not in ALLOWED:
        return await query.answer()

    await query.answer()

    data = query.data
    head, _, rest = data.partition(":")

    if head in ("publish", "discard"):
        return await on_confirm(update, context, head, rest)

    if data == "cancel":
        return await on_cancel(update, context)

    if head == "q":
        # Risposta da pulsante rapido: tolgo i pulsanti dalla domanda
        await query.edit_message_reply_markup(reply_markup=None)
        return await handle_answer(update, context, rest, typed=False)

    if head == "type":
        return await on_type(update, context, rest)

    if data == "newposter":
        return await poster_prompt(update, context)

    if head == "list":
        return await entries_screen(update, context, promos=(rest == "promos"))

    if head == "en":
        return await entry_screen(update, context, rest)

    if head == "ea":
        action, name = rest.split(":", 1)
        return await entry_action(update, context, action, name)

    if data == "menu":
        return await menu_screen(update, context)

    if head == "cat":
        return await category_screen(update, context, rest)

    if head == "it":
        category_id, index = rest.split(":")
        return await item_screen(update, context, category_id, int(index))

    if head == "ia":
        action, category_id, index = rest.split(":")
        return await item_action(update, context, action, category_id, int(index))

    if head == "add":
        return await add_start(update, context, rest)

    if head == "notice":
        return await notice_action(update, context, rest)

    if data == "more":
        return await more_screen(update, context)

    if head == "more":
        return await more_action(update, context, rest)

    if data == "undo:yes":
        return await undo_confirmed(update, context)

    log.warning("Pulsante sconosciuto: %s", data)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):

    log.exception("Errore", exc_info=context.error)

    if isinstance(update, Update) and update.effective_chat:
        await context.bot.send_message(
            update.effective_chat.id,
            f"⚠️ Qualcosa è andato storto: {context.error}",
        )


# =========================================================
# NOTIFICHE: bot aggiornato, sito modificato da fuori
# =========================================================

async def notify_all(bot, text):

    for user_id in ALLOWED:
        try:
            await bot.send_message(
                user_id, text, parse_mode="HTML", disable_web_page_preview=True,
            )
        except Exception:
            log.warning("Notifica non consegnata a %s", user_id)


def bot_changes():
    """Novità del bot dall'ultima versione notificata (None se nulla da dire)."""

    state = core.read_state()
    last = state.get("bot_version")

    state["bot_version"] = RUNNING_VERSION
    core.write_state(state)

    if last == RUNNING_VERSION:
        return None

    if last and core.is_ancestor(last, RUNNING_VERSION):
        commits = core.commits_between(last, RUNNING_VERSION)
    else:
        # Prima volta (o cronologia riscritta): solo l'ultima novità
        commits = core.commits_between(f"{RUNNING_VERSION}~1", RUNNING_VERSION)

    return [c["subject"] for c in commits if any(f.startswith("bot/") for f in c["files"])]


def is_site_change(commit):
    """Commit che cambia il sito visibile (non solo bot, script o file tecnici)."""

    technical = ("bot/", "tools/", "CNAME", ".gitignore", ".nojekyll")

    return commit["author"] != core.BOT_AUTHOR and any(
        not f.startswith(technical) for f in commit["files"]
    )


def site_changes():
    """Modifiche al sito fatte fuori dal bot dall'ultimo controllo."""

    state = core.read_state()
    last = state.get("site_seen")
    current = core.fetch_origin()

    state["site_seen"] = current
    core.write_state(state)

    if not last or last == current or not core.is_ancestor(last, current):
        return []

    return [c for c in core.commits_between(last, current) if is_site_change(c)]


async def check_site(context: ContextTypes.DEFAULT_TYPE):

    try:
        commits = await in_repo(site_changes)
    except Exception:
        log.exception("Controllo modifiche al sito fallito")
        return

    if not commits:
        return

    lines = "\n".join(f"• {esc(c['subject'])} <i>({esc(c['author'])})</i>" for c in commits[-10:])

    await notify_all(
        context.bot,
        f"🌐 <b>Sito aggiornato</b>\n{lines}\n\nSarà visibile tra 1-2 minuti: {SITE_URL}",
    )


async def post_init(app: Application):

    await app.bot.set_my_commands([
        BotCommand("start", "Mostra i pulsanti"),
        BotCommand("menu", "Modifica il menu"),
        BotCommand("eventi", "Eventi sul sito"),
        BotCommand("promozioni", "Promozioni sul sito"),
        BotCommand("avviso", "Avviso in fondo al sito"),
        BotCommand("aiuto", "Come si usa"),
        BotCommand("stop", "Interrompe la domanda in corso"),
    ])

    news = await in_repo(bot_changes)

    if news:
        lines = "\n".join(f"• {esc(subject)}" for subject in news[-10:])
        await notify_all(app.bot, f"🔄 <b>Bot aggiornato</b>\n{lines}\n\nScrivi /start se non vedi i pulsanti nuovi.")

    # Il primo controllo segnala anche le modifiche fatte mentre il bot era spento
    app.job_queue.run_repeating(check_site, interval=SITE_CHECK_SECONDS, first=20)


def main():

    if not ENV.get("TELEGRAM_TOKEN") or not ALLOWED:
        raise SystemExit("Configura TELEGRAM_TOKEN e ALLOWED_USER_IDS in .env")

    app = (
        Application.builder()
        .token(ENV["TELEGRAM_TOKEN"])
        .post_init(post_init)
        .build()
    )

    commands = {
        "start": cmd_start,
        "aiuto": cmd_help,
        "help": cmd_help,
        "stop": cmd_stop,
        "menu": menu_screen,
        "eventi": lambda u, c: entries_screen(u, c, promos=False),
        "promozioni": lambda u, c: entries_screen(u, c, promos=True),
        "avviso": cmd_notice,
        "annulla": lambda u, c: more_action(u, c, "undo"),
        "stato": lambda u, c: more_action(u, c, "log"),
        "prezzo": cmd_price,
        "nascondi": cmd_hide,
        "mostra": cmd_show,
    }

    for name, handler in commands.items():
        app.add_handler(CommandHandler(name, handler, filters=AUTH))

    app.add_handler(MessageHandler(AUTH & (filters.PHOTO | filters.Document.IMAGE), on_photo))
    app.add_handler(MessageHandler(AUTH & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))

    app.add_error_handler(on_error)

    log.info("Bot avviato")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
