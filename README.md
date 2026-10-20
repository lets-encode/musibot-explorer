# musibot-explorer

Scripts for exploring the Musibot OMR service (https://quest.ms.mff.cuni.cz/musibot/)
before anything is integrated into Let's Encode. Nothing here is used by the
instigation platform.

## Setup

```bash
cd musibot-explorer
uv sync                      # installs httpx, pillow, verovio, musibot-core, musibot-client
cp .env.example .env         # put your API token in MUSIBOT_TOKEN
```

Without a token the scripts mint a public session, which the server caps at one
execution at a time, five pages, a 60 s execution timeout and one hour of life.

## The explorer (start here)

```bash
uv run explorer.py          # then open http://127.0.0.1:8765
```

A local page for calling the API by hand with your own images, one step at a
time, with every request and response shown. `explorer.py` is only the relay:
it holds the token from `.env` (Musibot sends no CORS headers, so a browser
page cannot call it directly), forwards API calls and the presigned
upload/download transfers, renders MusicXML and MEI to SVG with verovio, and
saves a page's files to `out/<page_id>/` on request.

What the page does:

1. **Page**: create a page, upload your own files (or one from `samples/`) to
   a path in it, delete it, save everything to `out/`. Several files with `{n}`
   in the path are numbered. PNG is re-encoded as JPEG.
2. **Pipelines**: the live listing in three groups. *Full pipelines*
   (`mzk-page` 1 and 2) take `image.jpg` and run every step in one execution.
   *Steps, one at a time* are the same work as separate executions, numbered
   in the order they run: 1 `dvorak-ola` (layout), 2 `pmcg-slice` (crops),
   3 `mzk-staff` (transcription), 4 `pmcg-glue` 1 or 2 (page MusicXML). *Models
   on their own* are the staff models, one crop per execution. Each entry says
   whether the page holds its inputs or names what is missing. Clicking an entry
   opens it: its signature, the page's files for each input pattern (checkboxes
   for `{*}` sets, a single choice for `{name}` slots), parameters as JSON, a
   note when it would write over files already in the page, and the start
   button.
3. **Executions**: every execution of the page with its state and duration.

The **Files** list shows which execution (and pipeline) wrote each file. The
**Viewer** shows images with the page's `layout.json` drawn over `image.jpg`
(loaded and redrawn whenever a new one is written or uploaded; toggle categories
in the legend), JSON and XML as text, and MusicXML or MEI rendered as notation
(converted locally by verovio; the MEI is appended in the Text tab). Errors
appear in the header bar.

The **Timeline** is one chronological list, oldest first: every request the
page sends with its status, a one-line reading of the response and the raw
bodies behind *details*; the three server streams being opened; and, indented
under the execution they belong to, the log lines the models print, each file
the server reports as written, the polling while waiting, and the final result
with the list of files that execution produced.

Pages you create stay on the server under your token until you delete them
(or the server evicts them), so use *Delete page* when you are done with one.

## Other ways to look at the service

1. **The Musibot web UI** at https://quest.ms.mff.cuni.cz/musibot/ — the
   developers' own page, on a public session (caps above).
2. **The interactive API docs** at https://quest.ms.mff.cuni.cz/musibot/api/docs —
   click *Authorize*, paste the token, and call endpoints by hand. File bytes
   go to presigned URLs, which the docs page cannot transfer for you.
3. **The scripts below**, for the same steps from the command line.

## The scripts

```bash
uv run musibot_http.py pipelines                              # what the server offers
uv run musibot_http.py run samples/wellcome-b13047450-p2.jpg  # mzk-page: page in, MusicXML out
uv run musibot_http.py run IMAGE --pipeline dvorak-ola --version 2.0-2025-03-09   # layout only
uv run musibot_http.py steps samples/wellcome-b13047450-p2.jpg  # layout, crop locally, transcribe each staff
uv run musibot_client.py run samples/wellcome-b13047450-p2.jpg  # the same through the official client
uv run postprocess.py overlay out/<page>/image.jpg out/<page>/layout.json   # boxes drawn on the scan
uv run postprocess.py to-mei out/<page>/transcription.musicxml              # verovio MusicXML → MEI
uv run corrected_glue.py layout score.mei layout-corrected.json 1 > layout.json  # a piece's corrected layout as layout.json
GH_TOKEN=$(gh auth token -u ohwjdev) uv run corrected_glue.py run ohwjdev/REPO sources/piece-01 1 --fill-failed  # slice, mzk-staff, glue 2
```

`musibot_http.py` prints every request and response to stderr (`--quiet` hides
it) and the page's log stream to stdout. Outputs land in `out/<page_id>/`;
`run` and `steps` also write `layout.png` (the layout boxes over the scan) and
`.mei` next to each `.musicxml`.

`steps` is the interesting one for Let's Encode: it does what `mzk-page` does,
but with the cropping done locally from `layout.json`. That is the seam where a
corrected layout, or a different grouping of staves into systems, could be
inserted before transcription. Since 2026-10-05 Musibot offers the same seam
itself: `pmcg-slice`, `mzk-staff` and `pmcg-glue` take an uploaded `layout.json`
(see the explorer's *Steps* group).

## What the service offers today (2026-10-20)

`api`, `core` and `python-client` are unchanged since 0.3.0 (2026-08-14). The
pipelines come from `pmcg-orchestrator` 0.2.0 (2026-10-05, formerly
`omniomr-orchestrator`); a pipeline's name and version are a contract written in
its source, and a version is bumped when the same input would come out
noticeably different.

| Name | Kind | Input | Output |
| --- | --- | --- | --- |
| `dvorak-ola` `2.0-2025-03-09` | model | `image.jpg` | `layout.json` — COCO boxes: `staff`, `system`, `grandstaff`, `staffMeasure`, `systemMeasure`, each with a confidence `score` |
| `ayce`, `ayce-long` `2026-08-03-…` | model | `Staves/{staff}/image.jpg` (one per execution) | `Staves/{staff}/transcription.musicxml` + `.lmx` |
| `mzk-page` `1` | full pipeline | `image.jpg` | `layout.json`, crop and transcription per staff, `transcription.musicxml` with every staff in one part, a system break per staff |
| `mzk-page` `2` | full pipeline | `image.jpg` | the same files; `transcription.musicxml` has a part per instrument, read from the `system` and `grandstaff` boxes |
| `pmcg-slice` `1` | step 2 | `image.jpg`, `layout.json` | `Staves/{*}/image.jpg` — crops with a margin of 0.9 × staff height, reading order |
| `mzk-staff` `1` | step 3 | `Staves/{*s}/image.jpg` (any number) | `Staves/{*s}/transcription.musicxml` (+ `.lmx`); fails only when every staff failed |
| `pmcg-glue` `1`, `2` | step 4 | `layout.json`, `Staves/{*}/image.jpg`, `Staves/{*}/transcription.musicxml` | `transcription.musicxml`, glued as `mzk-page` 1 or 2 |

Run on one page in the order 1–4, the steps write the same `transcription.musicxml`
as `mzk-page` of the glue version used; a corrected `layout.json` can be
uploaded between step 1 and step 2. `pmcg-glue` pairs the staff folders (sorted
by number) with the layout's `staff` boxes in reading order; a staff with a crop
and no transcription becomes a placeholder. `mzk-page` `2` / `pmcg-glue` `2`
zip a grand staff's two staves into one part, pad the staves of a system to the
same measure count, write an instrument missing from a system as hidden measure
rests and carry an unprinted clef, key and time over from that instrument's
previous staff. `dvorak-ola` takes `confidence`, `iou`, `image_size` (default
640; raise it for measures on dense pages) and `max_detections` as parameters.

Timings seen on the Liszt sample (9 staves): layout 1 s, slice 1 s, `mzk-staff`
over all nine 3 s, glue 1 s; `mzk-page` 2 about 4 s.

## Sample pages

`samples/` holds pages 2 and 3 of Wellcome Collection b13047450 (a 4-page
Meyerbeer song, public domain), fetched from its IIIF image service at full size,
and page 1 of Liszt's "Blume und Duft" (`liszt-blume-und-duft-p1.jpg`, with the
layout model's default-size output next to it): a clean engraving of 3 systems
× 3 staves on which the layout model finds every staff and measure.
