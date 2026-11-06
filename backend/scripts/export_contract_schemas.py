"""Обновление только схемы обмена Backend; общие контракты команды не переписываются."""

import json
from pathlib import Path

from pydantic.json_schema import models_json_schema

from app.review_contract import AnalysisV1, CheckRequest, CheckResult, PrepareRequest


def schemas():
    root = Path(__file__).resolve().parents[1]
    mapping, definitions = models_json_schema(
        [(model, "validation") for model in (PrepareRequest, AnalysisV1, CheckRequest, CheckResult)]
    )
    result = {
        root / "examples/review/contract.schema.json": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            **definitions,
            "oneOf": list(mapping.values()),
        }
    }
    return result


def main():
    for path, schema in schemas().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
