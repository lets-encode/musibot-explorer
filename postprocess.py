"""What to do with Musibot's output files once they are on disk.

    uv run postprocess.py overlay out/<page>/image.jpg out/<page>/layout.json
    uv run postprocess.py to-mei out/<page>/transcription.musicxml

`overlay` draws the boxes of layout.json (a COCO document) over the page scan,
one colour per category, and writes <layout>.png next to it. `to-mei` converts
a MusicXML file to MEI with verovio and writes <name>.mei next to it.
"""

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

COLOURS = {
    "staff": (220, 40, 40),
    "system": (40, 90, 220),
    "grandstaff": (40, 160, 80),
    "staffMeasure": (240, 160, 0),
    "systemMeasure": (150, 40, 200),
}
WIDTHS = {"system": 8, "grandstaff": 6, "staff": 4, "systemMeasure": 3, "staffMeasure": 2}


def overlay(image_path: Path, layout_path: Path) -> Path:
    layout = json.loads(layout_path.read_text())
    names = {c["id"]: c["name"] for c in layout.get("categories", [])}
    image = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    counts: dict[str, int] = {}
    for a in layout.get("annotations", []):
        name = names.get(a["category_id"], "?")
        x, y, w, h = a["bbox"]
        draw.rectangle([x, y, x + w, y + h], outline=COLOURS.get(name, (0, 0, 0)), width=WIDTHS.get(name, 2))
        counts[name] = counts.get(name, 0) + 1
    target = layout_path.with_suffix(".png")
    image.save(target)
    print(f"{target}: " + ", ".join(f"{n} {name}" for name, n in sorted(counts.items())))
    return target


def to_mei(musicxml_path: Path) -> Path:
    import verovio

    tk = verovio.toolkit()
    if not tk.loadData(musicxml_path.read_text()):
        raise SystemExit(f"verovio could not load {musicxml_path}")
    target = musicxml_path.with_suffix(".mei")
    target.write_text(tk.getMEI())
    print(f"{target}: {target.stat().st_size} bytes")
    return target


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["overlay", image, layout]:
            overlay(Path(image), Path(layout))
        case ["to-mei", musicxml]:
            to_mei(Path(musicxml))
        case _:
            raise SystemExit(__doc__)
