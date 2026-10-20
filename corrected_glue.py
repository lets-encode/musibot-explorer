"""A piece's corrected layout as Musibot's layout.json, and Musibot's own gluing on it.

    uv run corrected_glue.py layout SCORE_MEI LAYOUT_CORRECTED PAGE > layout.json
    GH_TOKEN=$(gh auth token -u ohwjdev) uv run corrected_glue.py run OWNER/REPO sources/piece-01 PAGE

`layout` turns one page of a Let's Encode piece (its `score.mei` and
`layout-corrected.json`) into the COCO document the layout model writes:
the `staff` and `grandstaff` boxes as they are, and one `system` box per system,
the union of that system's measure boxes. Systems are derived as `pageSystems`
in lets-encode/src/lib/omr-layout.ts derives them: the page's measure zones in
order, a new system at each zone the score's body starts with a `<pb/>` or
`<sb/>`. The corrected layout keeps no system boxes of its own.

`run` reads the piece from the campaign repo with `gh api` (read-only), writes
the layout, and runs on one new Musibot page `pmcg-slice 1` → `mzk-staff 1`
(all crops in one execution) → `pmcg-glue 2`. Everything the page holds is
saved to out/glue-<repo>-p<PAGE>/ and the page's log stream printed; the Musibot
page is deleted afterwards unless --keep is given. `pmcg-glue 2` fails the page
when one staff of a grand staff has no transcription; --fill-failed uploads a
one-measure rest for each such staff before gluing.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import musibot_http
from common import OUT_DIR, api_url, save, token

# One measure holding a measure rest: what --fill-failed gives a staff mzk-staff could not transcribe.
MEASURE_REST = (
    '<?xml version="1.0" encoding="UTF-8"?><score-partwise version="3.1"><part-list>'
    '<score-part id="P1"><part-name>Staff</part-name></score-part></part-list><part id="P1">'
    "<measure><attributes><divisions>1</divisions></attributes>"
    '<note><rest measure="yes"/><duration>4</duration><voice>1</voice></note></measure></part></score-partwise>'
)

ATTR = re.compile(r'([\w:]+)="([^"]*)"')


def attrs(tag: str) -> dict[str, str]:
    return dict(ATTR.findall(tag))


def page_starts(score: str) -> list[list[bool]]:
    """Per surface, per measure zone in document order: whether it starts a system (pb or sb)."""
    body = re.search(r"<body\b[^>]*>([\s\S]*?)</body>", score)
    starts: set[str] = set()
    pending = False
    for token in re.findall(r"<(?:pb|sb|measure)\b[^>]*>", body.group(1) if body else ""):
        if token.startswith(("<pb", "<sb")):
            pending = True
            continue
        facs = attrs(token).get("facs", "")
        if facs.startswith("#") and pending:
            starts.add(facs[1:])
        pending = False
    pages = []
    for surface in re.finditer(r"<surface\b[^>]*/>|<surface\b[^>]*>([\s\S]*?)</surface>", score):
        inner = surface.group(1) or ""
        if not re.search(r"<graphic\b", inner):
            continue
        zones = [attrs(z) for z in re.findall(r"<zone\b[^>]*>", inner)]
        pages.append([z.get("xml:id", "") in starts for z in zones if z.get("type") == "measure"])
    if not starts:
        raise SystemExit("The score's body has no <pb/> or <sb/>; systems cannot be derived.")
    return pages


def system_boxes(measures: list[dict], starts: list[bool]) -> list[dict]:
    """The union of each system's measure boxes; a system starts at index 0 and at each flagged zone."""
    systems: list[dict] = []
    for i, box in enumerate(measures):
        if i == 0 or starts[i]:
            systems.append(dict(box))
        else:
            row = systems[-1]
            row["ulx"] = min(row["ulx"], box["ulx"])
            row["uly"] = min(row["uly"], box["uly"])
            row["lrx"] = max(row["lrx"], box["lrx"])
            row["lry"] = max(row["lry"], box["lry"])
    return systems


def coco_layout(score: str, corrected: dict, page: int) -> dict:
    """Page `page` (1-based) of the corrected layout as a COCO layout document."""
    starts = page_starts(score)[page - 1]
    entry = corrected["pages"][page - 1]
    if len(starts) != len(entry["measures"]):
        raise SystemExit(f"Page {page}: {len(entry['measures'])} measure boxes, {len(starts)} measure zones.")
    graphic = attrs(re.findall(r"<graphic\b[^>]*>", score)[page - 1])
    categories = [{"id": 0, "name": "staff"}, {"id": 2, "name": "grandstaff"}, {"id": 3, "name": "system"}]
    annotations = []
    groups = [
        (0, entry["staves"]),
        (2, entry["grandstaves"]),
        (3, system_boxes(entry["measures"], starts)),
    ]
    for category, boxes in groups:
        for b in boxes:
            w, h = b["lrx"] - b["ulx"], b["lry"] - b["uly"]
            annotations.append({
                "id": len(annotations),
                "image_id": 0,
                "category_id": category,
                "segmentation": [[b["ulx"], b["uly"], b["ulx"], b["lry"], b["lrx"], b["lry"], b["lrx"], b["uly"]]],
                "area": w * h,
                "bbox": [b["ulx"], b["uly"], w, h],
                "iscrowd": 0,
                "score": 1.0,
            })
    return {
        "info": {"description": "Let's Encode layout-corrected.json, systems from the measure zones and breaks"},
        "images": [{"id": 0, "width": int(graphic["width"]), "height": int(graphic["height"]), "file_name": "image.jpg"}],
        "annotations": annotations,
        "categories": categories,
    }


def gh_raw(repo: str, path: str) -> bytes:
    """A file of the campaign repo at its default branch, through `gh api` (GH_TOKEN as the shell sets it)."""
    return subprocess.run(
        ["gh", "api", f"repos/{repo}/contents/{path}", "-H", "Accept: application/vnd.github.raw"],
        check=True, capture_output=True,
    ).stdout


def resolve(piece: str, target: str) -> str:
    parts: list[str] = []
    for part in [*piece.split("/"), *target.split("/")]:
        if part == "..":
            parts.pop()
        elif part and part != ".":
            parts.append(part)
    return "/".join(parts)


def cmd_layout(args) -> None:
    score = Path(args.score).read_text()
    corrected = json.loads(Path(args.corrected).read_text())
    json.dump(coco_layout(score, corrected, args.page), sys.stdout, indent=1)


def cmd_run(args) -> None:
    score = gh_raw(args.repo, f"{args.piece}/score.mei").decode()
    corrected = json.loads(gh_raw(args.repo, f"{args.piece}/layout-corrected.json"))
    layout = coco_layout(score, corrected, args.page)
    target = attrs(re.findall(r"<graphic\b[^>]*>", score)[args.page - 1])["target"]
    image = gh_raw(args.repo, resolve(args.piece, target))
    folder = OUT_DIR / f"glue-{args.repo.split('/')[-1]}-p{args.page}"
    folder.mkdir(parents=True, exist_ok=True)
    layout_bytes = json.dumps(layout, indent=1).encode()
    save(folder, "image.jpg", image)
    save(folder, "layout.json", layout_bytes)
    save(folder, "score.mei", score.encode())
    save(folder, "layout-corrected.json", json.dumps(corrected).encode())

    url = api_url()
    api = musibot_http.Api(url, token(url), quiet=True)
    page_id = api.create_page()
    print(f"Musibot page {page_id}")
    try:
        api.upload(page_id, {"image.jpg": image, "layout.json": layout_bytes})
        api.watch_logs(page_id)

        def step(name: str, version: str, inputs: list[str]) -> None:
            execution = api.wait(page_id, api.start(page_id, name, version, inputs))
            print(f"{name} {version}: {execution['state']} in {execution['seconds']} s"
                  + (f" ({execution['error']})" if execution.get("error") else ""))

        step("pmcg-slice", "1", ["image.jpg", "layout.json"])
        crops = sorted((f["path"] for f in api.list_files(page_id) if f["path"].endswith("/image.jpg")),
                       key=lambda p: int(p.split("/")[1]))
        step("mzk-staff", "1", crops)
        musicxml = [f["path"] for f in api.list_files(page_id) if f["path"].endswith("/transcription.musicxml")]
        if args.fill_failed:
            failed = [c.replace("image.jpg", "transcription.musicxml") for c in crops]
            failed = [p for p in failed if p not in musicxml]
            if failed:
                print(f"filling {', '.join(failed)} with a measure rest")
                api.upload(page_id, {p: MEASURE_REST.encode() for p in failed})
                musicxml += failed
        step("pmcg-glue", "2", ["layout.json", *crops, *musicxml])
        files = [f["path"] for f in api.list_files(page_id)]
        for path, content in api.download(page_id, files).items():
            save(folder, path, content)
        print(f"{len(files)} files in {folder}")
    finally:
        if args.keep:
            print(f"kept Musibot page {page_id}")
        else:
            api.delete_page(page_id)
            print(f"deleted Musibot page {page_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    layout = sub.add_parser("layout", help="one page's corrected layout as a COCO layout.json, to stdout")
    layout.add_argument("score")
    layout.add_argument("corrected")
    layout.add_argument("page", type=int)
    layout.set_defaults(func=cmd_layout)
    run = sub.add_parser("run", help="slice, transcribe and glue (version 2) one page on Musibot")
    run.add_argument("repo", help="OWNER/REPO of the campaign")
    run.add_argument("piece", help="the piece's directory, e.g. sources/piece-01")
    run.add_argument("page", type=int)
    run.add_argument("--fill-failed", action="store_true",
                     help="give staves without a transcription a one-measure rest before gluing")
    run.add_argument("--keep", action="store_true", help="do not delete the Musibot page")
    run.set_defaults(func=cmd_run)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
