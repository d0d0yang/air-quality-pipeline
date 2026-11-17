"""
pull_published.py

Used by the pages workflow: downloads the dashboard json from blob storage's publish/ container with a read-only
SAS URL, checks each file, and writes it into frontend/data/. Prints nothing secret.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from pipeline.schema import SCHEMA, validate

OUT = Path(__file__).resolve().parent.parent / "frontend" / "data"


def file_url(sas_url, name):
    # the secret is a container url with a query string: insert the blob name before the query
    base, _, query = sas_url.partition("?")
    return f"{base.rstrip('/')}/{urllib.parse.quote(name)}?{query}"


def main():
    sas_url = os.environ.get("PUBLISHED_DATA_SAS_URL", "").strip()
    if not sas_url:
        print("no PUBLISHED_DATA_SAS_URL, keeping the committed snapshot")
        return 0
    payloads = {}
    for name in SCHEMA:
        with urllib.request.urlopen(file_url(sas_url, name), timeout=60) as resp:
            payloads[name] = json.loads(resp.read())
        validate(name, payloads[name])
    # only write once every file downloaded and passed, so a half-published day never lands
    OUT.mkdir(parents=True, exist_ok=True)
    for name, payload in payloads.items():
        (OUT / name).write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        print(f"updated {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
