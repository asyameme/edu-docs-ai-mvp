"""Предметные таблицы v0.5 и перенос проекций всех сохранённых версий."""

from uuid import UUID, uuid5

import sqlalchemy as sa
from alembic import op

from app.domain import SnapshotData, SnapshotV2
from app.relational_storage import insert_projection

revision = "0004_relational"
down_revision = "0003_originals_bytea"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE entities (
            version_id uuid NOT NULL REFERENCES program_versions(id), id uuid NOT NULL,
            kind text NOT NULL CHECK(length(trim(kind))>0),
            attributes jsonb NOT NULL CHECK(jsonb_typeof(attributes)='object'),
            PRIMARY KEY(version_id,id)
        );
        CREATE INDEX entities_kind_idx ON entities(version_id,kind);
        CREATE TABLE relations (
            version_id uuid NOT NULL REFERENCES program_versions(id), id uuid NOT NULL,
            source_entity_id uuid NOT NULL, target_entity_id uuid NOT NULL,
            type text NOT NULL CHECK(length(trim(type))>0), matrix_value smallint,
            attributes jsonb NOT NULL DEFAULT '{}'::jsonb
                CHECK(jsonb_typeof(attributes)='object'),
            PRIMARY KEY(version_id,id),
            FOREIGN KEY(version_id,source_entity_id) REFERENCES entities(version_id,id),
            FOREIGN KEY(version_id,target_entity_id) REFERENCES entities(version_id,id),
            CHECK(matrix_value IS NULL OR (type='FORMS_COMPETENCY' AND matrix_value IN (0,1)))
        );
        CREATE INDEX relations_source_idx ON relations(version_id,source_entity_id,type);
        CREATE INDEX relations_target_idx ON relations(version_id,target_entity_id,type);
        ALTER TABLE source_links DISABLE TRIGGER immutable_links;
        ALTER TABLE source_refs DISABLE TRIGGER immutable_refs;
        DELETE FROM source_links;
        DELETE FROM source_refs;
        ALTER TABLE source_links DROP COLUMN target_kind, DROP COLUMN target_id;
        ALTER TABLE source_links ADD COLUMN entity_id uuid, ADD COLUMN relation_id uuid;
        ALTER TABLE source_links ADD CHECK ((entity_id IS NOT NULL AND relation_id IS NULL)
            OR (entity_id IS NULL AND relation_id IS NOT NULL));
        ALTER TABLE source_links ADD FOREIGN KEY(version_id,entity_id)
            REFERENCES entities(version_id,id);
        ALTER TABLE source_links ADD FOREIGN KEY(version_id,relation_id)
            REFERENCES relations(version_id,id);
        CREATE UNIQUE INDEX links_entity_idx
            ON source_links(version_id,entity_id,field_path,source_ref_id);
        CREATE UNIQUE INDEX links_relation_idx
            ON source_links(version_id,relation_id,field_path,source_ref_id);
    """)
    # Снимки остаются неизменными; проекции пересоздаются атомарно из их содержимого.
    connection = op.get_bind()
    raw = connection.connection.driver_connection
    with raw.cursor() as cur:
        for row in connection.execute(sa.text("SELECT id,snapshot FROM program_versions")):
            if row.snapshot.get("schema_version") == 2:
                snapshot = SnapshotV2.model_validate(row.snapshot)
            else:
                # Историческая миграция читает прежние поля; текущий API принимает только V2.
                data = {k: v for k, v in row.snapshot.items() if k != "schema_version"}
                snapshot = SnapshotData.model_validate(data)
                refs = {ref.id: uuid5(UUID(str(row.id)), str(ref.id))
                        for ref in snapshot.source_refs}
                for ref in snapshot.source_refs:
                    ref.id = refs[ref.id]
                for item in [*snapshot.entities, *snapshot.relations]:
                    item.source_ref_ids = [refs[key] for key in item.source_ref_ids]
            insert_projection(cur, row.id, snapshot)
    op.execute("""
        ALTER TABLE source_links ENABLE TRIGGER immutable_links;
        ALTER TABLE source_refs ENABLE TRIGGER immutable_refs;
        CREATE TRIGGER immutable_entities BEFORE UPDATE OR DELETE ON entities
            FOR EACH ROW EXECUTE FUNCTION reject_original_mutation();
        CREATE TRIGGER immutable_relations BEFORE UPDATE OR DELETE ON relations
            FOR EACH ROW EXECUTE FUNCTION reject_original_mutation();
        ALTER TABLE processing_jobs DROP CONSTRAINT processing_jobs_type_check;
        ALTER TABLE processing_jobs ADD CONSTRAINT processing_jobs_type_check
            CHECK(type IN ('import','validate','generate'));
    """)


def downgrade():
    raise RuntimeError("Откат предметной схемы требует восстановления проверенной резервной копии")
