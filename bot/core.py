"""
Parte "tecnica" del bot: file del sito, git, modifiche e anteprime.

Ogni modifica è un dizionario ("change") che apply_change() applica ai
file. Si applica due volte: per l'anteprima (poi scartata) e alla
conferma, su una copia appena allineata a GitHub.
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


def last_bot_commit():

    sync()

    author, subject, commit = git("log", "-1", "--format=%an%x09%s%x09%h").split("\t")

    return (subject, commit) if author == BOT_AUTHOR else None


def revert_last():

    sync()

    subject = git("log", "-1", "--format=%s")

    git("revert", "--no-edit", "HEAD")
    git("commit", "--quiet", "--amend", "-m", f"Annullato: {subject}")
    git("push", "--quiet", "origin", "main")

    return git("rev-parse", "--short", "HEAD")


def recent_changes(count=8):

    sync()

    return git(
        "log", f"-{count}",
        "--format=%cd · %an · %s",
        "--date=format:%d/%m %H:%M",
    )


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
            events.append({
                "date": change["date"],
                "time": change["time"],
                "title": change["title"],
                "image": image,
            })
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

    if kind == "event":
        return "index.html", "#eventi"

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
