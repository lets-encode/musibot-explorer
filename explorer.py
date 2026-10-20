"""A local page for calling Musibot by hand: uv run explorer.py, then open http://127.0.0.1:8765.

The browser page (explorer.html) does every API step itself and shows each
request and response. This process is only the same-origin relay the page
needs: it holds the token (Musibot sends no CORS headers and the token must
not sit in a page), forwards API calls and presigned-URL transfers, renders
MusicXML/MEI to SVG with verovio, and saves a page's files to out/<page_id>/.
"""

import json
import mimetypes
import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import verovio
from flask import Flask, Response, request, stream_with_context

from common import api_url, out_dir, save, token

HERE = Path(__file__).parent
URL = api_url()
TOKEN = token(URL)
HOST = urlsplit(URL).netloc
AUTH = {"Authorization": f"Bearer {TOKEN}"}

app = Flask(__name__)
http = httpx.Client(timeout=120)

# One toolkit for the process: a toolkit created inside a request thread does
# not find its font resources. It is not thread-safe, so calls are serialised.
verovio_toolkit = verovio.toolkit()
verovio_toolkit.setOptions({"scale": 35, "adjustPageHeight": True, "adjustPageWidth": True, "pageWidth": 2400, "header": "none", "footer": "none"})
verovio_lock = threading.Lock()


@app.get("/")
def page() -> Response:
    return Response((HERE / "explorer.html").read_text(), mimetype="text/html")


@app.get("/samples")
def samples() -> Response:
    """The files in samples/, offered by the page as an alternative to the file picker."""
    names = sorted(f.name for f in (HERE / "samples").iterdir() if f.is_file() and not f.name.startswith("."))
    return Response(json.dumps(names), mimetype="application/json")


@app.get("/samples/<name>")
def sample(name: str) -> Response:
    file = HERE / "samples" / Path(name).name
    return Response(file.read_bytes(), mimetype=mimetypes.guess_type(name)[0] or "application/octet-stream")


@app.get("/info")
def info() -> Response:
    """Where the relay points and whether the token is an issued one or a public session."""
    import os
    return Response(json.dumps({"url": URL, "public_session": not os.environ.get("MUSIBOT_TOKEN")}), mimetype="application/json")


@app.route("/api/<path:path>", methods=["GET", "POST", "DELETE"])
def relay(path: str) -> Response:
    """Forward one API call, adding the token. SSE responses are streamed through."""
    body = request.get_data() or None
    headers = dict(AUTH)
    if body:
        headers["Content-Type"] = "application/json"
    upstream = http.send(http.build_request(request.method, f"{URL}/{path}", headers=headers, content=body, timeout=None), stream=True)
    content_type = upstream.headers.get("content-type", "")
    if content_type.startswith("text/event-stream"):
        def events():
            try:
                for chunk in upstream.iter_raw():
                    yield chunk
            finally:
                upstream.close()
        return Response(stream_with_context(events()), status=upstream.status_code, mimetype="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})
    upstream.read()
    upstream.close()
    return Response(upstream.content, status=upstream.status_code, mimetype=content_type or "application/octet-stream")


@app.route("/blob", methods=["GET", "PUT"])
def blob() -> Response:
    """Transfer bytes to or from a presigned object-storage URL on the Musibot host."""
    url = request.args.get("url", "")
    if urlsplit(url).netloc != HOST:
        return Response("only presigned URLs on the Musibot host are relayed", status=400)
    if request.method == "PUT":
        upstream = http.put(url, content=request.get_data())
        return Response(upstream.text, status=upstream.status_code)
    upstream = http.get(url)
    guessed = mimetypes.guess_type(urlsplit(url).path)[0] or "application/octet-stream"
    return Response(upstream.content, status=upstream.status_code, mimetype=guessed)


@app.post("/render")
def render() -> Response:
    """MusicXML or MEI in; MEI and one SVG per page out."""
    with verovio_lock:
        tk = verovio_toolkit
        if not tk.loadData(request.get_data(as_text=True)):
            return Response(json.dumps({"error": "verovio could not load the data"}), status=422, mimetype="application/json")
        svgs = [tk.renderToSVG(n) for n in range(1, tk.getPageCount() + 1)]
        return Response(json.dumps({"mei": tk.getMEI(), "svg": svgs}), mimetype="application/json")


@app.post("/save/<page_id>")
def save_page(page_id: str) -> Response:
    """Download everything the page holds into out/<page_id>/."""
    files = http.get(f"{URL}/musicorpus-pages/{page_id}/files", headers=AUTH).json()["files"]
    paths = [f["path"] for f in files]
    urls = http.post(f"{URL}/musicorpus-pages/{page_id}/file-urls", headers=AUTH, json={"get": paths}).json()["get"]
    folder = out_dir(page_id)
    for path in paths:
        save(folder, path, http.get(urls[path]).content)
    return Response(json.dumps({"folder": str(folder), "files": paths}), mimetype="application/json")


if __name__ == "__main__":
    print(f"Musibot explorer: http://127.0.0.1:8765  (relaying to {URL})")
    app.run(host="127.0.0.1", port=8765, threaded=True)
