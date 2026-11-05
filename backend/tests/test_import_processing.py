"""Настоящая сеть HTTP, БД и Temporal с тестовым поставщиком результата парсера."""

import io
import json
import os
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("PARSER_FIXTURE_TESTS") != "1", reason="Needs test parser server"),
]


def archive(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as package:
        for name, content in files.items():
            package.writestr(name, content)
    return stream.getvalue()


def wait(http, path):
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline:
        response = http.get(path)
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] not in {"pending", "processing"}:
            return job
        time.sleep(0.2)
    pytest.fail("Задача не перешла в конечное состояние")


@pytest.mark.parametrize(
    "files,expected",
    [
        ({"valid.docx": b"valid"}, "completed"),
        ({"warning.xlsx": b"warning"}, "partial"),
        ({"ok.docx": b"valid", "bad.xlsx": b"broken"}, "partial"),
        ({"bad.docx": b"broken"}, "failed"),
        ({"empty.docx": b"empty"}, "failed"),
        ({"wrong.docx": b"wrong-hash"}, "failed"),
        ({"retry.docx": b"transient-" + uuid4().bytes}, "completed"),
        ({"unavailable.docx": b"unavailable-" + uuid4().bytes}, "failed"),
    ],
)
def test_processing_outcomes_preserve_originals_and_versions(files, expected):
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        program = http.post("/api/v1/programs", json={"name": "Контракт обработки"}).json()
        path = f"/api/v1/programs/{program['id']}/imports"
        accepted = http.post(
            path,
            files={"file": ("package.zip", archive(files))},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert accepted.status_code == 202, accepted.text
        job_path = path + "/" + accepted.json()["id"]
        job = wait(http, job_path)
        assert job["status"] == expected, job
        for content in files.values():
            if content.startswith((b"transient", b"unavailable")):
                calls = httpx.get(
                    "http://parser-fixture:8090/calls/" + sha256(content).hexdigest(),
                    trust_env=False,
                ).json()
                assert calls["count"] == (3 if content.startswith(b"unavailable") else 2)
                assert len(set(calls["keys"])) == 1
        assert http.get(f"/api/v1/programs/{program['id']}").json() == program
        for item in job["files"]:
            downloaded = http.get(
                f"/api/v1/programs/{program['id']}/documents/{item['document_id']}/download"
            )
            assert downloaded.content == files[item["member_name"]]
        result = http.get(job_path + "/result")
        if expected == "failed":
            assert result.status_code == 409
        else:
            assert result.status_code == 200, result.text
            assert result.json()["entities"]
            assert len(result.json()["document_ids"]) == len(files)
        if any(f["status"] == "failed" for f in job["files"]):
            retry = http.post(job_path + "/retry")
            assert retry.status_code == 202
            assert retry.json()["attempt"] == 2
            retried = wait(http, job_path)
            assert retried["status"] == expected
            assert len(http.get(f"/api/v1/programs/{program['id']}/documents").json()) == len(files)


def test_concurrent_duplicate_and_reused_key_are_consistent():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        program = http.post("/api/v1/programs", json={"name": "Параллельные загрузки"}).json()
        path = f"/api/v1/programs/{program['id']}/imports"
        content = archive({"same.docx": b"same"})
        keys = [uuid4().hex, uuid4().hex]

        def send(key):
            return http.post(
                path,
                files={"file": ("same.zip", content)},
                headers={"Idempotency-Key": key},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(send, keys))
        assert all(response.status_code == 202 for response in responses)
        assert responses[0].json()["id"] == responses[1].json()["id"]
        changed = archive({"different.docx": b"different"})
        for key in keys:
            response = http.post(
                path,
                files={"file": ("different.zip", changed)},
                headers={"Idempotency-Key": key},
            )
            assert response.status_code == 409, response.text


def test_sse_stream_delivers_state_and_reconnects_to_latest_result():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        program = http.post("/api/v1/programs", json={"name": "Проверка SSE"}).json()
        path = f"/api/v1/programs/{program['id']}/imports"
        accepted = http.post(
            path,
            files={"file": ("package.zip", archive({"sse.docx": b"sse"}))},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert accepted.status_code == 202, accepted.text
        events = path + "/" + accepted.json()["id"] + "/events"
        states = []
        with http.stream("GET", events) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            for line in response.iter_lines():
                if line.startswith("data: "):
                    states.append(json.loads(line[6:]))
        assert states[-1]["status"] == "completed"
        assert [s["progress"] for s in states] == sorted(s["progress"] for s in states)
        repeated = http.get(events, headers={"Last-Event-ID": "old-state"})
        assert repeated.status_code == 200
        assert '"completed"' in repeated.text
        assert http.get(path + "/" + str(uuid4()) + "/events").status_code == 404
