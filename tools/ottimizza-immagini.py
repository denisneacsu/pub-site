#!/usr/bin/env python3
"""
Ottimizza le immagini del sito.

Legge gli originali da images-src/ e scrive in images/ le versioni
leggere da pubblicare, mantenendo la stessa struttura di cartelle.

- Foto e locandine (senza trasparenza) -> .jpg
- Immagini con trasparenza (es. logo)  -> .png, bordi trasparenti rimossi
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


def optimize(source):

    relative = source.relative_to(SRC)

    image = ImageOps.exif_transpose(Image.open(source))

    transparent = has_transparency(image)

    if transparent:
        image = image.convert("RGBA")
        image = image.crop(image.getchannel("A").getbbox())
    else:
        image = image.convert("RGB")

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
