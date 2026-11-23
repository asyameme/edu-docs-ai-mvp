"""Упаковка документов для проверки выполняет один пакетный поиск артефактов."""

from hashlib import sha256
from uuid import UUID, uuid4, uuid5

from app.graph_storage import canonical_bytes
from app.review_repository import pack_check_documents


class ArtifactLookupCursor:
    def __init__(self):
        self.calls = 0
        self.parameters = None

    def execute(self, query, parameters):
        self.calls += 1
        self.parameters = parameters
        return self

    def fetchall(self):
        documents, hashes, _program_id = self.parameters
        return [
            {
                "document_id": document_id,
                "sha256": checksum,
                "id": uuid5(UUID(str(document_id)), checksum),
            }
            for document_id, checksum in zip(documents, hashes, strict=True)
        ]


def test_check_document_artifacts_are_packed_in_one_query():
    program_id = uuid4()
    documents = [
        {
            "document_id": str(uuid4()),
            "parsed": {"meta": {"document_number": index}, "rows": [index, False]},
            "original_url": f"/documents/{index}",
        }
        for index in range(78)
    ]
    cur = ArtifactLookupCursor()

    packed = pack_check_documents(cur, program_id, documents)

    assert cur.calls == 1
    assert len(packed) == 78
    for source, result in zip(documents, packed, strict=True):
        checksum = sha256(canonical_bytes(source["parsed"])).hexdigest()
        assert result == {
            **source,
            "parsed": {"__parsed_artifact": str(uuid5(UUID(source["document_id"]), checksum))},
        }
