"""Обновление JSON Schema из общих исполняемых моделей контрактов."""

import json
from pathlib import Path

from pydantic.json_schema import models_json_schema

from app.review_contract import AnalysisV1, CheckRequest, CheckResult, PrepareRequest
from contracts.document_v2.models import MODELS
from contracts.parser_output import ParserOutput


def schemas():
    root = Path(__file__).resolve().parents[1]
    result = {root / "contracts/parser_contract.schema.json": ParserOutput.model_json_schema()}
    for kind, model in MODELS.items():
        result[root / f"contracts/document_v2/schemas/{kind.lower()}.schema.json"] = (
            model.model_json_schema()
        )
    mapping, definitions = models_json_schema(
        [(model, "validation") for model in (PrepareRequest, AnalysisV1, CheckRequest, CheckResult)]
    )
    result[root / "examples/review/contract.schema.json"] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **definitions,
        "oneOf": list(mapping.values()),
    }
    return result


def main():
    for path, schema in schemas().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
