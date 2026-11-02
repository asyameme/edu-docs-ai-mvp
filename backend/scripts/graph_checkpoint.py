"""Контроль настоящих узлов и рёбер после пересоздания контейнеров."""

import json
import sys
import time
from uuid import uuid4

import httpx


def main():
    with httpx.Client(base_url="http://frontend", timeout=20, trust_env=False) as http:
        if sys.argv[1] == "prepare":
            response = http.post(
                "/api/v1/programs", json={"name": "Искусственная проверка сохранности графа"}
            )
            response.raise_for_status()
            program = response.json()
            root = "/api/v1/programs/" + program["id"]
            first, second, third = [str(uuid4()) for _ in range(3)]
            snapshot = {
                "entities": [
                    {
                        "id": first,
                        "kind": "discipline",
                        "attributes": {
                            "code": "001",
                            "credits": "3.000000000000000001",
                            "unknown": None,
                        },
                    },
                    {"id": second, "kind": "competency", "attributes": {"code": "ОПК-1"}},
                    {"id": third, "kind": "indicator", "attributes": {"code": "ОПК-1.1"}},
                ],
                "relations": [
                    {
                        "id": str(uuid4()),
                        "type": "FORMS_COMPETENCY",
                        "source_id": first,
                        "target_id": second,
                    },
                    {
                        "id": str(uuid4()),
                        "type": "HAS_INDICATOR",
                        "source_id": second,
                        "target_id": third,
                    },
                ],
            }
            response = http.post(
                root + "/versions",
                json={"expected_version_id": program["current_version_id"], "snapshot": snapshot},
                headers={"Idempotency-Key": uuid4().hex},
            )
            response.raise_for_status()
            version = response.json()
            base = root + "/versions/" + version["id"] + "/graph"
            response = http.post(base + "/builds", headers={"Idempotency-Key": uuid4().hex})
            response.raise_for_status()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if http.get(base).status_code == 200:
                    break
                time.sleep(0.2)
            else:
                raise TimeoutError("Граф контрольной версии не построен")
            state = {
                "base": base,
                "entity_id": first,
                "manifest": http.get(base).json(),
                "entities": http.get(base + "/entities").json(),
                "relations": http.get(base + "/relations").json(),
            }
            assert state["manifest"]["node_count"] == 3
            assert state["manifest"]["relation_count"] == 2
            assert (
                http.get(base + "/entities/" + first + "/neighbors", params={"depth": 2}).json()[
                    "total"
                ]
                == 2
            )
            print(json.dumps(state, ensure_ascii=True))
        else:
            state = json.loads(sys.stdin.read().lstrip("\ufeff"))
            base = state["base"]
            assert http.get(base).json() == state["manifest"]
            assert http.get(base + "/entities").json() == state["entities"]
            assert http.get(base + "/relations").json() == state["relations"]
            assert (
                http.get(
                    base + "/entities/" + state["entity_id"] + "/neighbors", params={"depth": 2}
                ).json()["total"]
                == 2
            )
            print("Граф сохранён: точные атрибуты, три узла, два ребра и обход глубины2")


if __name__ == "__main__":
    main()
