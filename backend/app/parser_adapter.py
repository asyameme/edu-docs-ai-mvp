"""Преобразование документов V2 в снимок наблюдений без выбора между расхождениями."""

from app.document_adapter import to_snapshot_v2


def to_snapshot(program_id, files):
    return to_snapshot_v2(program_id, files)
