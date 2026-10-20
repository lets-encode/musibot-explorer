"""The same round trips through the official Python client, for comparison.

    uv run musibot_client.py pipelines
    uv run musibot_client.py run samples/wellcome-b13047450-p2.jpg      # process_page, one call
    uv run musibot_client.py steps samples/wellcome-b13047450-p2.jpg    # the step methods

Set MUSIBOT_TOKEN to use an issued token; without it a public session is minted.
"""

import logging
import sys
from pathlib import Path

from musibot.client import MusibotClient, PipelineExecutionFailed

import postprocess
from common import api_url, out_dir, save, token

logging.basicConfig(level=logging.INFO, format="[client] %(message)s", stream=sys.stderr)


def cmd_pipelines(client: MusibotClient) -> None:
    for p in client.list_pipelines().pipelines:
        kind = "model   " if p.implicit else "pipeline"
        print(f"{kind}  {p.name:12} {p.version:26} instances={p.instances}  {p.signature.input} -> {p.signature.output}")


def cmd_run(client: MusibotClient, image: Path) -> None:
    # One call: create, upload, run, download, delete. The predicate form of
    # `output` fetches whatever the pipeline produced, staff folders included.
    try:
        files = client.process_page(
            input={"image.jpg": image.read_bytes()},
            pipeline=("mzk-page", "1"),
            output=lambda f: True,
        )
    except PipelineExecutionFailed as failure:
        raise SystemExit(f"mzk-page failed on page {failure.page_id}: {failure.error}")
    folder = out_dir("client-run")
    for path, content in files.items():
        save(folder, path, content)
        print(f"  {path:40} {len(content):>9} bytes")
    postprocess.overlay(folder / "image.jpg", folder / "layout.json")
    postprocess.to_mei(folder / "transcription.musicxml")


def cmd_steps(client: MusibotClient, image: Path) -> None:
    # Holding a page open across executions: the client's step methods map one
    # to one onto the HTTP endpoints (see musibot_http.py).
    page = client.create_page()
    try:
        client.upload_files(page.page_id, {"image.jpg": image.read_bytes()})
        execution = client.start_execution(page.page_id, "dvorak-ola", "2.0-2025-03-09", ["image.jpg"])
        settled = client.wait_for_execution(page.page_id, execution.execution_id)
        print(f"layout: {settled.state} {settled.error or ''}")
        for f in client.list_files(page.page_id):
            print(f"  {f.path:40} {f.size:>9} bytes")
        folder = out_dir(page.page_id)
        for path, content in client.download_files(page.page_id, ["image.jpg", "layout.json"]).items():
            save(folder, path, content)
        postprocess.overlay(folder / "image.jpg", folder / "layout.json")
    finally:
        client.delete_page(page.page_id)


def main() -> None:
    url = api_url()
    with MusibotClient(musibot_api_url=url, api_token=token(url)) as client:
        match sys.argv[1:]:
            case ["pipelines"]:
                cmd_pipelines(client)
            case ["run", image]:
                cmd_run(client, Path(image))
            case ["steps", image]:
                cmd_steps(client, Path(image))
            case _:
                raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
