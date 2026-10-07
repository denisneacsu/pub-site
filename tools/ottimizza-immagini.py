#!/usr/bin/env python3
"""
Ottimizza le immagini del sito.

Legge gli originali da images-src/ e scrive in images/ le versioni
leggere da pubblicare, mantenendo la stessa struttura di cartelle.

- Foto e locandine (senza trasparenza) -> .jpg
- Immagini con trasparenza (es. logo)  -> .png, bordi trasparenti rimossi
- Locandine (events/): bande nere degli screenshot rimosse
- Dal logo vengono generate anche le favicon
- Rotazione EXIF applicata, metadati rimossi (niente GPS dai telefoni)

Uso, dalla cartella del progetto:

    .venv/bin/python tools/ottimizza-immagini.py

Per sostituire una foto: mettere il nuovo originale in images-src/
con lo stesso nome (estensione qualsiasi) e rilanciare lo script.
"""

from pathlib import Path

from PIL import Image, ImageOps


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "images-src"
OUT = ROOT / "images"

EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

JPEG_QUALITY = 80

# Sfondo delle icone (uguale a --bg del CSS)
ICON_BACKGROUND = (17, 16, 15)

# Sotto questa luminosità (0-255) un bordo è considerato "nero"
BLACK_THRESHOLD = 24


def max_width(relative_path):
    """Larghezza massima in base al tipo di immagine."""

    if relative_path.parts[0] == "events":
        return 1000

    if relative_path.stem == "logo":
        return 400

    return 1600


def has_transparency(image):

    if image.mode in ("RGBA", "LA"):
        return image.getchannel("A").getextrema()[0] < 255

    return image.mode == "P" and "transparency" in image.info


def trim_black_borders(image):
    """Rimuove le bande nere intorno alle locandine (screenshot)."""

    mask = image.convert("L").point(lambda value: 255 if value > BLACK_THRESHOLD else 0)

    box = mask.getbbox()

    return image.crop(box) if box else image


def make_icons(logo):
    """Favicon e icona per la schermata home di iPhone/Android."""

    for name, size, padding in (
        ("favicon-32.png", 32, 0),
        ("apple-touch-icon.png", 180, 18),
        ("icon-512.png", 512, 48),
    ):

        icon = Image.new("RGBA", (size, size), ICON_BACKGROUND + (255,))

        inner = size - padding * 2

        mark = logo.copy()
        mark.thumbnail((inner, inner), Image.LANCZOS)

        icon.alpha_composite(
            mark,
            ((size - mark.width) // 2, (size - mark.height) // 2),
        )

        icon.convert("RGB").save(OUT / name, optimize=True)


def optimize(source):

    relative = source.relative_to(SRC)

    image = ImageOps.exif_transpose(Image.open(source))

    transparent = has_transparency(image)

    if transparent:
        image = image.convert("RGBA")
        image = image.crop(image.getchannel("A").getbbox())

        if relative.stem == "logo":
            make_icons(image)
    else:
        image = image.convert("RGB")

        if relative.parts[0] == "events":
            image = trim_black_borders(image)

    limit = max_width(relative)

    if image.width > limit:
        height = round(image.height * limit / image.width)
        image = image.resize((limit, height), Image.LANCZOS)

    target = OUT / relative.with_suffix(".png" if transparent else ".jpg")
    target.parent.mkdir(parents=True, exist_ok=True)

    if transparent:
        image.save(target, optimize=True)
    else:
        image.save(
            target,
            quality=JPEG_QUALITY,
            optimize=True,
            progressive=True,
        )

    return relative, target, source.stat().st_size, target.stat().st_size


def main():

    if not SRC.is_dir():
        raise SystemExit(f"Cartella non trovata: {SRC}")

    sources = sorted(
        path for path in SRC.rglob("*")
        if path.suffix.lower() in EXTENSIONS
    )

    total_before = total_after = 0

    for source in sources:

        relative, target, before, after = optimize(source)

        total_before += before
        total_after += after

        print(
            f"{str(relative):45} {before / 1024:8.0f} KB -> "
            f"{after / 1024:6.0f} KB  {target.relative_to(ROOT)}"
        )

    print(
        f"\nTotale: {total_before / 1024 / 1024:.1f} MB -> "
        f"{total_after / 1024 / 1024:.1f} MB"
    )


if __name__ == "__main__":
    main()
