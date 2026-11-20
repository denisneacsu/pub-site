"""
Ordini dei tavoli: cosa è stato ordinato a ogni tavolo aperto.

Gli ordini stanno in ~/memphis-bot/tavoli.json, fuori dal repository
del sito: non vanno mai su GitHub e non toccano il sito.

Ogni riga dell'ordine ricorda categoria, nome del prodotto e, per i
prodotti con due prezzi (birre piccola/media, vini calice/bottiglia),
quale dei due. Il prezzo si legge ogni volta dal menu: se cambia nel
menu, cambia anche negli ordini aperti. Il prezzo salvato serve solo
se il prodotto non esiste più nel menu (rinominato o eliminato).
"""

import json
import os
from datetime import datetime

from core import BOT_HOME, TZ, categories


TABLES_FILE = BOT_HOME / "tavoli.json"

TABLE_COUNT = 15


# =========================================================
# FILE
# =========================================================

def read_tables():
    try:
        return json.loads(TABLES_FILE.read_text())
    except (OSError, ValueError):
        return {}


def write_tables(tables):
    """Scrittura atomica: un file a metà non deve mai perdere gli ordini."""

    tmp = TABLES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tables, ensure_ascii=False, indent=4) + "\n")
    os.replace(tmp, TABLES_FILE)


def get_lines(tables, table):
    return tables.get(str(table), {}).get("items", [])


# =========================================================
# PREZZI DAL MENU
# =========================================================

def menu_index():
    """{(categoria, nome): (categoria, voce)} per trovare i prezzi attuali."""

    return {
        (category["id"], item["name"]): (category, item)
        for category in categories()
        for item in category["items"]
    }


def unit_price(item, variant):
    """Prezzo di una voce del menu (variant = indice per i doppi prezzi)."""

    if "prices" in item:
        prices = item["prices"]
        return prices[variant] if variant is not None and variant < len(prices) else None

    return item.get("price")


def variant_label(category, variant):

    if variant is None:
        return None

    columns = category.get("columns", [])

    return columns[variant] if variant < len(columns) else None


def priced_lines(lines, index=None):
    """
    Righe dell'ordine con il prezzo attuale del menu:
    [{name, label, qty, price, total}] (price None = senza prezzo).
    """

    index = index if index is not None else menu_index()
    result = []

    for line in lines:

        found = index.get((line["cat"], line["name"]))

        if found:
            category, item = found
            price = unit_price(item, line.get("variant"))
            label = variant_label(category, line.get("variant")) or line.get("label")
        else:
            price = line.get("price")
            label = line.get("label")

        result.append({
            "name": line["name"],
            "label": label,
            "qty": line["qty"],
            "price": price,
            "total": (price or 0) * line["qty"],
        })

    return result


def table_total(lines, index=None):
    return sum(line["total"] for line in priced_lines(lines, index))


# =========================================================
# MODIFICHE AGLI ORDINI
# =========================================================

def add_item(table, category, item, variant=None):
    """Aggiunge 1 al tavolo; restituisce la quantità ora in ordine."""

    tables = read_tables()

    entry = tables.setdefault(str(table), {
        "opened": datetime.now(TZ).isoformat(timespec="seconds"),
        "items": [],
    })

    for line in entry["items"]:
        if line["cat"] == category["id"] and line["name"] == item["name"] and line.get("variant") == variant:
            line["qty"] += 1
            line["price"] = unit_price(item, variant)
            break
    else:
        line = {
            "cat": category["id"],
            "name": item["name"],
            "variant": variant,
            "label": variant_label(category, variant),
            "price": unit_price(item, variant),
            "qty": 1,
        }
        entry["items"].append(line)

    write_tables(tables)

    return line["qty"]


def change_qty(table, position, delta):
    """+1 / -1 su una riga; a 0 la riga sparisce. False se la riga non c'è più."""

    tables = read_tables()
    entry = tables.get(str(table))

    if not entry or not 0 <= position < len(entry["items"]):
        return False

    line = entry["items"][position]
    line["qty"] += delta

    if line["qty"] <= 0:
        del entry["items"][position]

    if not entry["items"]:
        del tables[str(table)]

    write_tables(tables)

    return True


def close_table(table):
    """Libera il tavolo (per ora non si tiene lo storico)."""

    tables = read_tables()

    if tables.pop(str(table), None) is not None:
        write_tables(tables)
