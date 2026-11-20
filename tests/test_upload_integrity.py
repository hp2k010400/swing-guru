"""A video is analysed only once it has fully arrived (staging regression, 2026-11-07).

Staging's local storage used to write the upload in place, so /start called mid-upload saw a partial
file as present and the worker analysed the first 131071 bytes ("no swing found"). Uploads now land
atomically, and /start refuses while fewer bytes are stored than the app declared.
"""

from __future__ import annotations

import io
from urllib.parse import urlparse

import pytest

from swingguru.service.storage import LocalStorage
from test_service import A, FakeRunner, service  # noqa: F401  (fixture)

KEY = "user-aaaaaaaa/0b9f1c2e-1111-4a5b-9c8d-123456789abc/source.mp4"
BODY = b"\x00\x00\x00\x18ftypmp42" + b"x" * 6000


def _create(client, size: int) -> dict:
    created = client.post("/v1/analysis/jobs", headers=A, json={"file_name": "IMG_0001.MOV", "content_type": "video/mp4",
                                                                "size_bytes": size})
    assert created.status_code == 201, created.text
    return created.json()


def _put(client, data: dict, body: bytes):
    url = urlparse(data["upload"]["url"])
    return client.put(f"{url.path}?{url.query}", content=body, headers=data["upload"]["headers"])


def test_start_refused_until_every_declared_byte_is_stored(service):  # noqa: F811
    client, app, runner = service()
    data = _create(client, len(BODY))
    job_id = data["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A).json()["error"]["code"] == "upload_missing"
    # What /start used to see mid-upload: some bytes, not all of them.
    assert _put(client, data, BODY[:3000]).status_code == 200
    early = client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A)
    assert early.status_code == 409 and early.json()["error"]["code"] == "upload_missing"
    assert client.get(f"/v1/analysis/jobs/{job_id}", headers=A).json()["status"] == "awaiting_upload"
    assert app.state.worker.run_once() is False                      # nothing was queued
    # The full upload then starts normally, and the worker gets every byte.
    assert _put(client, data, BODY).status_code == 200
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A).json()["status"] == "queued"
    assert app.state.worker.run_once() is True
    assert runner.sizes[-1] == len(BODY)


def test_interrupted_write_leaves_no_object(tmp_path):
    from dataclasses import replace

    from swingguru.service.config import Settings

    storage = LocalStorage(replace(Settings(), data_dir=tmp_path))

    class Broken(io.RawIOBase):
        def __init__(self):
            self.sent = 0

        def read(self, n=-1):
            if self.sent:
                raise ConnectionError("client went away")
            self.sent = 1
            return b"x" * 1000

    with pytest.raises(ConnectionError):
        storage.write_stream(KEY, Broken(), 5000, 10_000)
    assert storage.size(KEY) is None
    assert not list((tmp_path).rglob("*.part"))
    storage.write_stream(KEY, io.BytesIO(b"y" * 5000), 5000, 10_000)
    assert storage.size(KEY) == 5000


def test_put_leaves_no_side_files(service, tmp_path):  # noqa: F811
    client, _, _ = service()
    data = _create(client, len(BODY))
    assert _put(client, data, BODY).status_code == 200
    assert not list(tmp_path.rglob("*.part"))


