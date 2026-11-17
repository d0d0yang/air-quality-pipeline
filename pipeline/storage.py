"""
storage.py

Same small interface for a local folder and Azure Blob Storage. Paths look like
"landing/naps_hourly/12/item.csv"; the first part is the blob container (or a subfolder locally).
"""

import csv
import io
import os
import tempfile
from pathlib import Path

from pipeline import config


def _split(path):
    container, _, name = path.strip("/").partition("/")
    if not name:
        raise ValueError(f"path needs a container and a name: {path!r}")
    return container, name


class LocalStorage:
    def __init__(self, root=None):
        self.root = Path(root or config.LOCAL_DATA_DIR)

    def full_path(self, path):
        return self.root / path.strip("/")

    def write_bytes(self, path, data):
        target = self.full_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return path

    def write_file(self, path, local_file):
        target = self.full_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(local_file, target)
        return path

    def read_bytes(self, path):
        return self.full_path(path).read_bytes()

    def exists(self, path):
        return self.full_path(path).exists()

    def list(self, prefix):
        base = self.full_path(prefix)
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.root)).replace("\\", "/") for p in base.rglob("*") if p.is_file())


class BlobStorage:
    def __init__(self, connection_string=None):
        from azure.storage.blob import BlobServiceClient

        conn = connection_string or config.AZURE_STORAGE_CONNECTION_STRING
        if not conn:
            raise RuntimeError("STORAGE_MODE=blob needs AZURE_STORAGE_CONNECTION_STRING")
        self.service = BlobServiceClient.from_connection_string(conn)

    def _blob(self, path):
        container, name = _split(path)
        return self.service.get_blob_client(container, name)

    def write_bytes(self, path, data):
        self._blob(path).upload_blob(data, overwrite=True)
        return path

    def write_file(self, path, local_file):
        with open(local_file, "rb") as fh:
            self._blob(path).upload_blob(fh, overwrite=True)
        os.remove(local_file)
        return path

    def read_bytes(self, path):
        return self._blob(path).download_blob().readall()

    def exists(self, path):
        return self._blob(path).exists()

    def list(self, prefix):
        container, _, name = prefix.strip("/").partition("/")
        client = self.service.get_container_client(container)
        return sorted(f"{container}/{b.name}" for b in client.list_blobs(name_starts_with=name))


_storage = None


def get_storage():
    global _storage
    if _storage is None:
        _storage = BlobStorage() if config.STORAGE_MODE == "blob" else LocalStorage()
    return _storage


def write_csv(path, header, rows, storage=None):
    """
    Streams rows to a temp file (so memory stays flat), then hands it to storage.
    UTF-8, every field quoted, header first. Returns the number of data rows written.
    """
    storage = storage or get_storage()
    fd, tmp = tempfile.mkstemp(suffix=".csv")
    count = 0
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])
            count += 1
    storage.write_file(path, tmp)
    return count


def read_csv(path, storage=None):
    storage = storage or get_storage()
    return list(csv.DictReader(io.StringIO(storage.read_bytes(path).decode("utf-8"))))
