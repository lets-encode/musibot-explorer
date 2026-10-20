"""Musibot over the raw HTTP API, one request at a time, each one printed.

    uv run musibot_http.py pipelines
    uv run musibot_http.py run samples/wellcome-b13047450-p2.jpg            # mzk-page 1
    uv run musibot_http.py run IMAGE --pipeline dvorak-ola --version 2.0-2025-03-09
    uv run musibot_http.py steps samples/wellcome-b13047450-p2.jpg          # layout, crop, transcribe

`run` is the whole round trip for one image: create page, upload, open the log
stream, start the pipeline, wait, list files, download everything, delete the
page. Outputs land in out/<page_id>/.

`steps` does the same work as mzk-page but with the seams exposed: it runs the
layout model alone, downloads layout.json, crops the staves locally, uploads
the crops, runs the staff pipeline once per staff, and downloads the results.
This is where a corrected layout could be inserted between the two models.

Set MUSIBOT_TOKEN to use an issued token; without it a public session is minted.
Pass --quiet to hide the request/response trace.
"""

import argparse
import io
import json
import sys
import threading
import time
from pathlib import Path

import httpx
from PIL import Image

import postprocess
from common import api_url, out_dir, save, token

STAFF_PADDING_RATIO = 0.9  # the same margin mzk-page uses when it crops
POLL_SECONDS = 1.0


class Api:
    """The handful of endpoints, with every request traced to stderr."""

    def __init__(self, url: str, bearer: str, quiet: bool):
        self.url = url
        self.headers = {"Authorization": f"Bearer {bearer}"}
        self.quiet = quiet
        self.http = httpx.Client(timeout=60)

    def call(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = self.http.request(method, self.url + path, headers=self.headers, **kwargs)
        if not self.quiet:
            body = kwargs.get("json")
            sent = f"  <- {json.dumps(body)}" if body is not None else ""
            print(f"[api] {method} {path} -> {response.status_code}{sent}", file=sys.stderr)
            if response.content and response.headers.get("content-type", "").startswith("application/json"):
                text = json.dumps(response.json())
                print(f"      {text[:400]}{'…' if len(text) > 400 else ''}", file=sys.stderr)
        if response.status_code >= 400:
            raise SystemExit(f"{method} {path} failed: {response.status_code} {response.text[:300]}")
        return response

    # --- the endpoints ---------------------------------------------------

    def pipelines(self) -> dict:
        return self.call("GET", "/pipelines").json()

    def create_page(self) -> str:
        return self.call("POST", "/musicorpus-pages").json()["page_id"]

    def delete_page(self, page_id: str) -> None:
        self.call("DELETE", f"/musicorpus-pages/{page_id}")

    def list_files(self, page_id: str) -> list[dict]:
        return self.call("GET", f"/musicorpus-pages/{page_id}/files").json()["files"]

    def upload(self, page_id: str, files: dict[str, bytes]) -> None:
        # The API only hands out presigned URLs; the bytes go straight to object storage.
        urls = self.call("POST", f"/musicorpus-pages/{page_id}/file-urls", json={"put": list(files)}).json()["put"]
        for path, content in files.items():
            r = self.http.put(urls[path], content=content)
            if not self.quiet:
                print(f"[s3]  PUT {path} ({len(content)} bytes) -> {r.status_code}", file=sys.stderr)
            r.raise_for_status()

    def download(self, page_id: str, paths: list[str]) -> dict[str, bytes]:
        urls = self.call("POST", f"/musicorpus-pages/{page_id}/file-urls", json={"get": paths}).json()["get"]
        out = {}
        for path in paths:
            r = self.http.get(urls[path])
            if not self.quiet:
                print(f"[s3]  GET {path} -> {r.status_code} ({len(r.content)} bytes)", file=sys.stderr)
            r.raise_for_status()
            out[path] = r.content
        return out

    def start(self, page_id: str, name: str, version: str, inputs: list[str], parameters: dict | None = None) -> int:
        body = {"pipeline_name": name, "pipeline_version": version, "input": inputs, "parameters": parameters or {}}
        return self.call("POST", f"/musicorpus-pages/{page_id}/pipeline-executions", json=body).json()["execution_id"]

    def wait(self, page_id: str, execution_id: int) -> dict:
        # Polling GET is the simplest way to wait; the streaming alternative is
        # POST /pipeline-execution-results (one SSE stream for all your pages).
        started = time.monotonic()
        while True:
            execution = self.call("GET", f"/musicorpus-pages/{page_id}/pipeline-executions/{execution_id}").json()
            if execution["state"] != "running":
                execution["seconds"] = round(time.monotonic() - started, 1)
                return execution
            time.sleep(POLL_SECONDS)

    def watch_logs(self, page_id: str) -> threading.Thread:
        """Print the page's log stream (SSE) from a background thread until the page is deleted."""

        def reader() -> None:
            try:
                with self.http.stream("POST", f"{self.url}/musicorpus-pages/{page_id}/logs", headers=self.headers, timeout=None) as r:
                    for line in r.iter_lines():
                        if line.startswith("data:"):
                            event = json.loads(line[5:])
                            print(f"[log] exec {event['execution_id']} {event['seconds']:6.1f}s {event['level']:7} {event['source']}: {event['message']}")
            except httpx.HTTPError:
                pass  # the page was deleted, which ends the stream

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        time.sleep(0.5)  # let the subscription exist before the first execution logs
        return thread


# --- commands -------------------------------------------------------------


def cmd_pipelines(api: Api, _args) -> None:
    listing = api.pipelines()
    for p in listing["pipelines"]:
        kind = "model   " if p["implicit"] else "pipeline"
        print(f"{kind}  {p['name']:12} {p['version']:26} instances={p['instances']}")
        print(f"          in : {', '.join(p['signature']['input'])}")
        print(f"          out: {', '.join(p['signature']['output'])}")
    for w in listing["warnings"]:
        print(f"warning: {w}")


def finish(api: Api, page_id: str, execution: dict) -> None:
    """Report the settled execution, download everything the page holds, delete the page."""
    print(f"execution {execution['execution_id']} {execution['state']} after {execution['seconds']}s" + (f": {execution['error']}" if execution.get("error") else ""))
    files = api.list_files(page_id)
    folder = out_dir(page_id)
    for path, content in api.download(page_id, [f["path"] for f in files]).items():
        save(folder, path, content)
    print(f"{len(files)} files in {folder}:")
    for f in sorted(files, key=lambda f: f["path"]):
        print(f"  {f['path']:40} {f['size']:>9} bytes")
    if (folder / "layout.json").exists():
        postprocess.overlay(folder / "image.jpg", folder / "layout.json")
    if (folder / "transcription.musicxml").exists():
        postprocess.to_mei(folder / "transcription.musicxml")


def cmd_run(api: Api, args) -> None:
    image = Path(args.image).read_bytes()
    # The signature decides where the upload lands: a page pipeline reads image.jpg,
    # a staff pipeline reads Staves/{s}/image.jpg.
    pipeline = next((p for p in api.pipelines()["pipelines"] if p["name"] == args.pipeline and p["version"] == args.version), None)
    if pipeline is None:
        raise SystemExit(f"{args.pipeline} {args.version} is not listed; run `pipelines`")
    input_path = pipeline["signature"]["input"][0].replace("{s}", "1").replace("{staff}", "1")

    page_id = api.create_page()
    try:
        api.upload(page_id, {input_path: image})
        api.watch_logs(page_id)
        execution_id = api.start(page_id, args.pipeline, args.version, [input_path], parameters=json.loads(args.parameters))
        finish(api, page_id, api.wait(page_id, execution_id))
    finally:
        if args.keep:
            print(f"page {page_id} kept on the server (it expires with the session)")
        else:
            api.delete_page(page_id)


def cmd_steps(api: Api, args) -> None:
    image = Path(args.image).read_bytes()
    page_id = api.create_page()
    folder = out_dir(page_id)
    try:
        api.upload(page_id, {"image.jpg": image})
        api.watch_logs(page_id)

        # 1. layout only
        print("--- step 1: layout model")
        execution = api.wait(page_id, api.start(page_id, args.layout_model, args.layout_version, ["image.jpg"]))
        if execution["state"] != "completed":
            raise SystemExit(f"layout failed: {execution['error']}")
        layout = json.loads(api.download(page_id, ["layout.json"])["layout.json"])
        save(folder, "image.jpg", image)
        save(folder, "layout.json", json.dumps(layout, indent=1).encode())
        postprocess.overlay(folder / "image.jpg", folder / "layout.json")

        # 2. crop the staves locally — the point where a corrected layout would go in
        print("--- step 2: crop staves locally")
        staff_ids = {c["id"] for c in layout["categories"] if c["name"] == "staff"}
        boxes = sorted((a["bbox"] for a in layout["annotations"] if a["category_id"] in staff_ids), key=lambda b: (b[1], b[0]))
        page = Image.open(io.BytesIO(image)).convert("RGB")
        crops: dict[str, bytes] = {}
        for n, (x, y, w, h) in enumerate(boxes, start=1):
            m = round(h * STAFF_PADDING_RATIO)
            crop = page.crop((max(0, x - m), max(0, y - m), min(page.width, x + w + m), min(page.height, y + h + m)))
            buffer = io.BytesIO()
            crop.save(buffer, "JPEG", quality=95)
            crops[f"Staves/{n}/image.jpg"] = buffer.getvalue()
        print(f"{len(crops)} staves")
        api.upload(page_id, crops)

        # 3. transcribe each staff — one execution per staff, as the signature says
        print("--- step 3: staff transcription, one execution per staff")
        for path in crops:
            execution = api.wait(page_id, api.start(page_id, args.staff_pipeline, args.staff_version, [path]))
            print(f"  {path}: {execution['state']} in {execution['seconds']}s" + (f" ({execution['error']})" if execution.get("error") else ""))

        # 4. download what there is now
        files = api.list_files(page_id)
        for path, content in api.download(page_id, [f["path"] for f in files]).items():
            save(folder, path, content)
        print(f"{len(files)} files in {folder}")
        for xml in sorted(folder.glob("Staves/*/transcription.musicxml")):
            postprocess.to_mei(xml)
    finally:
        api.delete_page(page_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--quiet", action="store_true", help="hide the request/response trace")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("pipelines", help="list what the server offers").set_defaults(func=cmd_pipelines)

    run = sub.add_parser("run", help="one image through one pipeline")
    run.add_argument("image")
    run.add_argument("--pipeline", default="mzk-page")
    run.add_argument("--version", default="1")
    run.add_argument("--parameters", default="{}", help="JSON object passed to the pipeline")
    run.add_argument("--keep", action="store_true", help="do not delete the page afterwards")
    run.set_defaults(func=cmd_run)

    steps = sub.add_parser("steps", help="layout, local cropping, per-staff transcription")
    steps.add_argument("image")
    steps.add_argument("--layout-model", default="dvorak-ola")
    steps.add_argument("--layout-version", default="2.0-2025-03-09")
    steps.add_argument("--staff-pipeline", default="mzk-staff")
    steps.add_argument("--staff-version", default="1")
    steps.set_defaults(func=cmd_steps)

    args = parser.parse_args()
    url = api_url()
    args.func(Api(url, token(url), args.quiet), args)


if __name__ == "__main__":
    main()
