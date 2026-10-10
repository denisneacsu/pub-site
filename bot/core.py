"""
Parte "tecnica" del bot: file del sito, git, modifiche e anteprime.

Ogni modifica è un dizionario ("change") che apply_change() applica ai
file. Le modifiche non vanno subito sul sito: si accumulano in una lista
comune (vedi MODIFICHE DA PUBBLICARE) e partono tutte con un solo push.
"""

import json
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


SITE = Path(__file__).resolve().parent.parent
BOT_HOME = SITE.parent
UPLOADS = BOT_HOME / "uploads"

LIVE_URLS = ("https://memphisristopub.it", "http://memphisristopub.it")

TZ = ZoneInfo("Europe/Rome")

BOT_AUTHOR = "Bot Memphis"


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


def categories():
    """Tutte le categorie del menu, nell'ordine del sito."""

    return [
        category
        for section in read_json("menu.json")["sections"]
        for category in section["categories"]
    ]


def get_category(menu, category_id):

    for section in menu["sections"]:
        for category in section["categories"]:
            if category["id"] == category_id:
                return category

    raise RuntimeError(f"Categoria non trovata: {category_id}")


def find_item(menu, category_id, name):

    for item in get_category(menu, category_id)["items"]:
        if item["name"] == name:
            return item

    raise RuntimeError(f"Voce non trovata: {name}")


def event_start(entry):
    """Inizio di un evento (senza orario: fine giornata, come sul sito)."""

    time = entry.get("time") or "23:59"

    return datetime.fromisoformat(f"{entry['date']}T{time}").replace(tzinfo=TZ)


def upcoming_events(events):
    """Eventi futuri, dal più vicino."""

    now = datetime.now(TZ)

    return sorted(
        (e for e in events if e.get("type") != "promo" and event_start(e) > now),
        key=event_start,
    )


def shown_featured(events):
    """
    Evento che il sito mostra in evidenza (riquadro grande con countdown):
    quello con la stella, altrimenti il più vicino. Stessa regola di events.js.
    """

    upcoming = upcoming_events(events)

    return next(
        (e for e in upcoming if e.get("featured")),
        upcoming[0] if upcoming else None,
    )


# Orari normali (uguali a js/hours.js): lunedì chiuso
NORMAL_OPENS = "19:00"
NORMAL_CLOSES = "02:30"


def service_date():
    """Data della serata in corso: fino alle 6 del mattino conta quella di ieri."""
    return (datetime.now(TZ) - timedelta(hours=6)).date()


def normally_open(day):
    return day.weekday() != 0


def read_hours():
    try:
        return read_json("hours.json")
    except (OSError, ValueError):
        return {"exceptions": []}


def get_exception(day):
    return next(
        (e for e in read_hours().get("exceptions", []) if e["date"] == day.isoformat()),
        None,
    )


def to_minutes(time):
    hours, minutes = map(int, time.split(":"))
    return hours * 60 + minutes


def valid_early_close(time):
    """Chiusura anticipata: dopo l'apertura e prima della chiusura normale."""

    opens = to_minutes(NORMAL_OPENS)
    normal = to_minutes(NORMAL_CLOSES) + 24 * 60

    end = to_minutes(time)
    if end <= opens:
        end += 24 * 60

    return opens < end < normal


def tonight_notice(change):
    if change.get("closed"):
        return "Stasera il locale è chiuso. Ci scusiamo per il disagio!"
    return f"Stasera chiudiamo alle {change['closes']}."


def get_entry(image):
    """Evento o promozione di events.json, cercato per immagine."""

    return next(
        (e for e in read_json("events.json") if e["image"] == image),
        None,
    )


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


def reset_to_origin():
    """Copia del bot uguale a GitHub, senza le modifiche in attesa."""

    git("fetch", "--quiet", "origin")
    git("reset", "--quiet", "--hard", "origin/main")
    git("clean", "--quiet", "-fd")


def discard_local():
    """Scarta i file toccati e non ancora committati."""

    git("reset", "--quiet", "--hard", "HEAD")
    git("clean", "--quiet", "-fd")


class AlreadyPublished(Exception):
    """La modifica non cambia nulla (es. conferma premuta due volte)."""


def commit_local(message, paths):

    git("add", "-A", "--", *paths)

    if not git("status", "--porcelain", "--", *paths):
        raise AlreadyPublished()

    git("commit", "--quiet", "-m", message)


def push():

    try:
        git("push", "--quiet", "origin", "HEAD:main")
    except RuntimeError:
        # Qualcuno ha pubblicato nel frattempo: ci si rimette in coda
        try:
            git("pull", "--quiet", "--rebase", "origin", "main")
        except RuntimeError:
            subprocess.run(["git", "rebase", "--abort"], cwd=SITE, capture_output=True)
            raise
        git("push", "--quiet", "origin", "HEAD:main")


def last_bot_commit():
    """Ultima modifica pubblicata, se fatta dal bot."""

    git("fetch", "--quiet", "origin")

    author, subject, commit = git("log", "-1", "--format=%an%x09%s%x09%h", "origin/main").split("\t")

    return (subject, commit) if author == BOT_AUTHOR else None


def with_author(message, by):
    """Descrizione della modifica con il nome di chi l'ha confermata."""
    return f"{message} ({by})" if by else message


def revert_last(by=None):
    """Annulla l'ultima modifica pubblicata (non tocca quelle in attesa)."""

    reset_to_origin()

    try:
        subject = git("log", "-1", "--format=%s")

        git("revert", "--no-edit", "HEAD")
        message = f"Annullato da {by}: {subject}" if by else f"Annullato: {subject}"

        git("commit", "--quiet", "--amend", "-m", message)
        git("push", "--quiet", "origin", "HEAD:main")

        return subject
    finally:
        rebuild()


# File che non cambiano il sito visibile (codice del bot, script, configurazione)
TECHNICAL_PATHS = ("bot/", "tools/", "CNAME", ".gitignore", ".nojekyll")


def touches_site(files):
    return any(not f.startswith(TECHNICAL_PATHS) for f in files)


def recent_changes(count=8):
    """Ultime modifiche al sito visibile, senza quelle solo tecniche."""

    git("fetch", "--quiet", "origin")

    out = git(
        "log", "-100", "origin/main",
        "--format=%x1e%cd%x09%an%x09%s",
        "--date=format:%d/%m %H:%M",
        "--name-only",
    )

    changes = []

    for block in out.split("\x1e"):

        lines = [line for line in block.strip().splitlines() if line]

        if not lines:
            continue

        when, author, subject = lines[0].split("\t", 2)

        if touches_site(lines[1:]):
            changes.append({"when": when, "author": author, "subject": subject})

        if len(changes) == count:
            break

    return changes


def head():
    return git("rev-parse", "HEAD")


def fetch_origin():
    """Scarica le novità da GitHub senza toccare i file; restituisce origin/main."""

    git("fetch", "--quiet", "origin")

    return git("rev-parse", "origin/main")


def is_ancestor(older, newer):

    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", older, newer],
        cwd=SITE,
        capture_output=True,
    )

    return result.returncode == 0


def commits_between(older, newer):
    """Commit da older (escluso) a newer, dal più vecchio, con i file toccati."""

    out = git(
        "log", "--reverse", f"{older}..{newer}",
        "--format=%x1e%h%x09%an%x09%s", "--name-only",
    )

    commits = []

    for block in out.split("\x1e"):

        lines = [line for line in block.strip().splitlines() if line]

        if not lines:
            continue

        sha, author, subject = lines[0].split("\t", 2)

        commits.append({
            "sha": sha,
            "author": author,
            "subject": subject,
            "files": lines[1:],
        })

    return commits


# =========================================================
# STATO DEL BOT (versione notificata, ultime modifiche viste)
# =========================================================

STATE_FILE = BOT_HOME / "state.json"


def read_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def write_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=4) + "\n")


# =========================================================
# MODIFICHE
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

    # --- Nuova locandina: evento o promozione ---

    if kind in ("event", "promo"):

        source_dir = SITE / "images-src" / "events"
        source_dir.mkdir(parents=True, exist_ok=True)

        upload = Path(change["upload"])
        shutil.copy(upload, source_dir / f"{change['stem']}{upload.suffix.lower()}")

        run_optimizer()

        image = published_image(change["stem"])

        events = read_json("events.json")

        if kind == "event":

            entry = {
                "date": change["date"],
                "time": change["time"],
                "title": change["title"],
                "image": image,
            }

            # "Pubblica in evidenza": la stella è una sola
            if change.get("featured"):
                for other in events:
                    other.pop("featured", None)
                entry["featured"] = True

            events.append(entry)
            message = f"Evento: {change['title']} ({format_date(change['date'])})"
        else:
            # Le promozioni nuove vanno per prime
            events.insert(0, {
                "type": "promo",
                "title": change["title"],
                "image": image,
                "until": change["until"],
            })
            message = f"Promozione: {change['title']}"

        write_json("events.json", events)

        return message, ["events.json", "images/events"]

    # --- Modifica di un evento o promozione esistente ---

    if kind == "entry_edit":

        events = read_json("events.json")

        entry = next(e for e in events if e["image"] == change["image"])
        entry.update(change["set"])

        write_json("events.json", events)

        return f"Modificato: {entry['title']}", ["events.json"]

    # --- Evidenza (stella): un solo evento alla volta ---

    if kind == "feature":

        events = read_json("events.json")

        for entry in events:
            entry.pop("featured", None)

        target = next(e for e in events if e["image"] == change["image"])

        if change["on"]:
            target["featured"] = True

        write_json("events.json", events)

        verb = "In evidenza" if change["on"] else "Tolta evidenza"

        return f"{verb}: {target['title']}", ["events.json"]

    # --- Rimozione di un evento o promozione ---

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

    # --- Menu ---

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

    if kind == "menu_add":

        menu = read_json("menu.json")

        get_category(menu, change["category"])["items"].append(change["item"])

        write_menu(menu)

        return f"Menu: aggiunto {change['item']['name']}", ["menu.json"]

    if kind == "menu_delete":

        menu = read_json("menu.json")

        category = get_category(menu, change["category"])
        category["items"] = [i for i in category["items"] if i["name"] != change["name"]]

        write_menu(menu)

        return f"Menu: eliminato {change['name']}", ["menu.json"]

    # --- Orario di una sola serata ---

    if kind == "tonight":

        day = date.fromisoformat(change["date"])
        yesterday = (today() - timedelta(days=1)).isoformat()

        # Le eccezioni passate non servono più
        exceptions = [
            e for e in read_hours().get("exceptions", [])
            if e["date"] >= yesterday and e["date"] != change["date"]
        ]

        if change.get("closes"):
            exceptions.append({"date": change["date"], "closes": change["closes"]})
            message = f"Orario {day:%d/%m}: chiusura alle {change['closes']}"
        elif change.get("closed"):
            exceptions.append({"date": change["date"], "closed": True})
            message = f"Orario {day:%d/%m}: serata chiusa"
        else:
            message = f"Orario {day:%d/%m}: orario normale"

        write_json("hours.json", {"exceptions": exceptions})

        if change.get("notice"):
            # L'avviso sparisce da solo la mattina dopo
            expires = datetime.combine(day + timedelta(days=1), datetime.min.time(), TZ) + timedelta(hours=6)
            write_json("notice.json", {"text": tonight_notice(change), "expires": expires.isoformat()})
        elif change.get("reset") and read_json("notice.json").get("expires"):
            write_json("notice.json", {"text": ""})

        return message, ["hours.json", "notice.json"]

    # --- Avviso ---

    if kind == "notice":

        write_json("notice.json", {"text": change["text"]})

        message = f"Avviso: {change['text']}" if change["text"] else "Avviso tolto"

        return message, ["notice.json"]

    raise ValueError(f"Modifica sconosciuta: {kind}")


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


PREVIEW_URL = None


def preview_target(change):
    """Pagina e parte di pagina da fotografare per l'anteprima."""

    kind = change["kind"]

    if kind in ("event", "feature"):
        return "index.html", "#eventi"

    if kind == "tonight":
        return "index.html", None

    if kind == "promo":
        return "index.html", "#promozioni"

    if kind == "entry_edit":
        entry = get_entry(change["image"])
        promo = entry and entry.get("type") == "promo"
        return "index.html", "#promozioni" if promo else "#eventi"

    if kind == "menu" and "featured" in change["set"]:
        return "index.html", "#menu"

    if kind in ("menu", "menu_add", "menu_delete"):
        return "menu.html", f"#{change['category']}"

    return "index.html", None


def screenshot(change):
    """Screenshot da telefono della parte di sito toccata dalla modifica."""

    global PREVIEW_URL

    if PREVIEW_URL is None:
        PREVIEW_URL = start_preview_server()

    page, selector = preview_target(change)

    with sync_playwright() as p:

        browser = p.chromium.launch()

        tab = browser.new_page(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2,
        )

        # Orario di stasera: anteprima come la vedrà chi apre il sito in serata
        if change["kind"] == "tonight":
            evening = datetime.fromisoformat(f"{change['date']}T18:00").replace(tzinfo=TZ)
            tab.clock.set_fixed_time(evening)

        tab.goto(f"{PREVIEW_URL}/{page}")
        tab.wait_for_timeout(1200)

        target = tab.locator(selector) if selector else None

        if target and target.count() and target.is_visible():
            image = target.screenshot()
        else:
            image = tab.screenshot()

        browser.close()

    return image


def preview(change):
    """Applica la modifica sopra quelle in attesa, fa lo screenshot e la scarta."""

    sync()

    try:
        apply_change(change)
        return screenshot(change)
    finally:
        discard_local()
        drop_source(change)


# =========================================================
# MODIFICHE DA PUBBLICARE
# =========================================================
#
# Le modifiche fatte col bot si accumulano in una lista comune a tutti
# gli utenti (state.json, "draft") e vanno sul sito insieme con
# "Pubblica modifiche": un solo push, un commit per modifica.
#
# Nella copia del bot ogni modifica in attesa è un commit locale sopra
# origin/main, così i pulsanti mostrano il sito come sarà dopo la
# pubblicazione. Se GitHub cambia (es. modifica a mano), la lista viene
# riapplicata sulla versione nuova; quelle non più applicabili si tolgono.
#
# Orario di stasera e Avviso sono urgenti: si pubblicano subito.

IMMEDIATE_KINDS = ("tonight", "notice")

# (origin/main, HEAD) dell'ultima ricostruzione: se non cambiano
# non serve riapplicare la lista
BUILT = None


def read_draft():
    return read_state().get("draft", [])


def write_draft(draft):

    state = read_state()
    state["draft"] = draft

    if not draft:
        state.pop("draft_reminded", None)

    write_state(state)


def drop_source(change):
    """Originale della locandina copiato in images-src: non deve restare."""

    if change["kind"] in ("event", "promo"):
        for source in (SITE / "images-src" / "events").glob(f"{change['stem']}.*"):
            source.unlink()


def mark_built():

    global BUILT

    BUILT = (git("rev-parse", "origin/main"), git("rev-parse", "HEAD"))


def rebuild():
    """
    Copia del bot = GitHub + modifiche in attesa, riapplicate da capo.
    Restituisce i messaggi di quelle non più applicabili (tolte dalla lista).
    """

    reset_to_origin()

    draft = read_draft()
    kept, skipped = [], []

    for change in draft:
        try:
            message, paths = apply_change(change)
            commit_local(with_author(message, change.get("by")), paths)
            kept.append(change)
        except Exception:
            # Già così su GitHub, oppure la voce non c'è più
            discard_local()
            drop_source(change)
            skipped.append(change["message"])

    if skipped:
        write_draft(kept)

        # Da far vedere in 🚀 Pubblica modifiche, anche se la
        # ricostruzione è partita da un'altra schermata
        state = read_state()
        state["draft_skipped"] = state.get("draft_skipped", []) + skipped
        write_state(state)

    mark_built()

    return skipped


def pop_skipped():
    """Modifiche tolte dalla lista perché non più applicabili (una volta sola)."""

    state = read_state()
    skipped = state.pop("draft_skipped", [])

    if skipped:
        write_state(state)

    return skipped


def sync():
    """Prepara la copia del bot; la ricostruisce solo se qualcosa è cambiato."""

    git("fetch", "--quiet", "origin")

    current = (git("rev-parse", "origin/main"), git("rev-parse", "HEAD"))

    if current == BUILT and not git("status", "--porcelain"):
        return []

    return rebuild()


def add_to_draft(change):
    """Applica la modifica nella copia del bot e la mette in lista."""

    sync()

    try:
        message, paths = apply_change(change)
        commit_local(with_author(message, change.get("by")), paths)
    except Exception:
        discard_local()
        raise

    entry = {k: v for k, v in change.items() if k != "back"}
    entry["message"] = message
    entry["added"] = datetime.now(TZ).isoformat(timespec="seconds")

    draft = read_draft() + [entry]
    write_draft(draft)

    mark_built()

    return message, len(draft)


def remove_from_draft(index, added):
    """Toglie una modifica; `added` evita di togliere quella sbagliata
    se nel frattempo la lista è cambiata."""

    draft = read_draft()

    if not 0 <= index < len(draft) or draft[index]["added"] != added:
        return None

    removed = draft.pop(index)

    write_draft(draft)
    drop_source(removed)
    rebuild()

    return removed["message"]


def reminder_due(after):
    """
    Modifiche in attesa da ricordare: l'ultima aggiunta è di almeno
    `after` fa e da allora non è partito un promemoria.
    """

    state = read_state()
    draft = state.get("draft") or []

    if not draft:
        return None

    last = max(datetime.fromisoformat(c["added"]) for c in draft)
    reminded = state.get("draft_reminded")

    if datetime.now(TZ) - last < after:
        return None

    if reminded and datetime.fromisoformat(reminded) >= last:
        return None

    state["draft_reminded"] = datetime.now(TZ).isoformat(timespec="seconds")
    write_state(state)

    return draft


def clear_draft():

    for change in read_draft():
        drop_source(change)

    write_draft([])
    rebuild()


def publish_draft():
    """
    Pubblica tutta la lista con un solo push. Restituisce (modifiche
    pubblicate, contenuto atteso dei JSON toccati, modifiche saltate).
    """

    skipped = rebuild()
    draft = read_draft()

    if not draft:
        return [], [], skipped

    files = git("diff", "--name-only", "origin/main", "HEAD").splitlines()

    push()

    write_draft([])
    mark_built()

    return draft, expected_json(files), skipped


def expected_json(files):
    """Contenuto dei JSON pubblicati, per controllare quando sono online."""
    return {f: read_json(f) for f in files if f.endswith(".json")}


def publish_now(change):
    """Orario di stasera e Avviso: subito sul sito, senza la lista in attesa."""

    reset_to_origin()

    try:
        message, paths = apply_change(change)
        commit_local(with_author(message, change.get("by")), paths)
        push()
        expected = expected_json(paths)
    finally:
        rebuild()

    return message, expected


def fetch_live(path):

    for base in LIVE_URLS:
        try:
            stamp = datetime.now().timestamp()
            with urlopen(f"{base}/{path}?t={stamp}", timeout=15) as response:
                return json.loads(response.read())
        except (ssl.SSLError, OSError, ValueError):
            continue

    return None


# =========================================================
# TESTO, DATE, PREZZI
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


WEEKDAYS = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]


def short_date(day):
    return f"{WEEKDAYS[day.weekday()]} {day:%d/%m}"


def parse_prices(tokens):
    """Prezzi in coda a una lista di parole: '6,50', '4 7', '- 5'."""

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


def item_price(item):
    """Prezzo di una voce del menu come testo."""

    if "prices" in item:
        return " / ".join(format_price(p) for p in item["prices"])

    return format_price(item.get("price"))


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
