"""Контрольная запись через API: оригиналы и история должны пережить пересоздание."""

import json
import sys
from hashlib import sha256
from uuid import uuid4

import httpx


def main():
    with httpx.Client(base_url="http://frontend", timeout=20, trust_env=False) as http:
        if sys.argv[1] == "prepare":
            result = http.post(
                "/api/v1/programs", json={"name": "Persistent acceptance checkpoint"}
            )
            result.raise_for_status()
            program = result.json()
            base = "/api/v1/programs/" + program["id"]
            content = b"Artificial original retained across container recreation"
            result = http.post(base + "/documents", files={"file": ("source.pdf", content)})
            result.raise_for_status()
            document = result.json()
            state = {
                "metadata": {"checkpoint": "persistent"},
                "entities": [
                    {
                        "id": str(uuid4()),
                        "kind": "discipline",
                        "attributes": {"hours": 108, "unknown": None},
                    }
                ],
                "document_ids": [document["id"]],
            }
            result = http.post(
                base + "/versions",
                json={"expected_version_id": program["current_version_id"], "snapshot": state},
                headers={"Idempotency-Key": str(uuid4())},
            )
            result.raise_for_status()
            print(
                json.dumps(
                    {
                        "program": program,
                        "document": document,
                        "version": result.json(),
                        "digest": sha256(content).hexdigest(),
                    }
                )
            )
            return
        state = json.loads(sys.stdin.read().lstrip("\ufeff"))
        base = "/api/v1/programs/" + state["program"]["id"]
        result = http.get(base)
        result.raise_for_status()
        assert result.json()["current_version_id"] == state["version"]["id"]
        result = http.get(base + "/versions/" + state["version"]["id"])
        result.raise_for_status()
        assert result.json() == state["version"]
        result = http.get(base + "/versions")
        result.raise_for_status()
        assert len(result.json()) == 2
        result = http.get(base + "/documents/" + state["document"]["id"] + "/download")
        result.raise_for_status()
        assert sha256(result.content).hexdigest() == state["digest"]
        print("Stage 2 persistence passed: program, snapshots, original bytes")


if __name__ == "__main__":
    main()
