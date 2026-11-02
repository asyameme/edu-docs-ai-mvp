"""Атомарная публикация графа версии и чтение с проверкой целостности."""

import json
import re
from collections import defaultdict

from neo4j import GraphDatabase, unit_of_work

from app.errors import DomainError
from app.graph_projection import canonical


def same_properties(actual, expected):
    # Обычное равенство Python смешивает False с0 и True с1, а графу важен тип.
    try:
        return canonical(actual) == canonical(expected)
    except (TypeError, ValueError):
        return False


def identifier(value):
    # Имена меток и типов получены только из проверенного преобразователя.
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,80}", value):
        raise ValueError("Invalid graph identifier")
    return "`" + value + "`"


class GraphStore:
    def __init__(self, settings):
        self.database = settings.neo4j_database
        self.driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
            connection_timeout=3,
            connection_acquisition_timeout=3,
            max_transaction_retry_time=0,
            max_connection_pool_size=5,
        )

    def close(self):
        self.driver.close()

    def ping(self):
        with self.driver.session(database=self.database) as session:
            session.execute_read(lambda tx: tx.run("RETURN 1 AS ok").single()["ok"])

    def initialize(self, relation_types=()):
        commands = [
            "CREATE CONSTRAINT entity_identity IF NOT EXISTS FOR (n:Entity) "
            "REQUIRE (n.program_id,n.version_id,n.entity_id) IS UNIQUE",
            "CREATE CONSTRAINT graph_version_identity IF NOT EXISTS FOR (n:ProgramVersionGraph) "
            "REQUIRE (n.program_id,n.version_id) IS UNIQUE",
        ]
        for kind in sorted(set(relation_types)):
            safe = identifier(kind)
            commands.append(
                f"CREATE CONSTRAINT {identifier('rel_' + kind)} IF NOT EXISTS "
                f"FOR ()-[r:{safe}]-() REQUIRE (r.program_id,r.version_id,r.relation_id) IS UNIQUE"
            )
        with self.driver.session(database=self.database) as session:
            for command in commands:
                session.run(command).consume()

    def publish(self, graph):
        self.initialize(edge["type"] for edge in graph["edges"])
        with self.driver.session(database=self.database) as session:
            session.execute_write(self._publish, graph)

    @staticmethod
    @unit_of_work(timeout=60)
    def _publish(tx, graph):
        scope = {"p": graph["program_id"], "v": graph["version_id"]}
        # Запись маркера сериализует конкурирующие сборки одной версии.
        tx.run(
            """MERGE (m:ProgramVersionGraph {program_id:$p,version_id:$v})
            SET m.generation=coalesce(m.generation,0)+1""",
            **scope,
        ).consume()
        tx.run("MATCH (n:Entity {program_id:$p,version_id:$v}) DETACH DELETE n", **scope).consume()
        groups = defaultdict(list)
        for item in graph["nodes"]:
            groups[item["label"]].append(item["props"])
        for label, rows in groups.items():
            tx.run(
                f"UNWIND $rows AS row CREATE (n:Entity:{identifier(label)}) SET n=row", rows=rows
            ).consume()
        groups = defaultdict(list)
        for item in graph["edges"]:
            groups[item["type"]].append(item)
        for kind, rows in groups.items():
            tx.run(
                f"""UNWIND $rows AS row
                MATCH (a:Entity {{program_id:$p,version_id:$v,entity_id:row.source_id}})
                MATCH (b:Entity {{program_id:$p,version_id:$v,entity_id:row.target_id}})
                CREATE (a)-[r:{identifier(kind)}]->(b) SET r=row.props""",
                **scope,
                rows=rows,
            ).consume()
        metadata = {key: value for key, value in graph.items() if key not in {"nodes", "edges"}}
        tx.run(
            "MATCH (m:ProgramVersionGraph {program_id:$p,version_id:$v}) SET m += $metadata",
            **scope,
            metadata=metadata,
        ).consume()
        GraphStore._read(tx, graph)

    def read(self, graph):
        with self.driver.session(database=self.database) as session:
            return session.execute_read(self._read, graph)

    def neighbors(
        self, graph, entity_id, depth, direction, relation_type, include_zero, limit, offset
    ):
        if direction not in {"out", "in", "both"} or type(depth) is not int or not 1 <= depth <= 3:
            raise ValueError("Invalid traversal")

        @unit_of_work(timeout=10)
        def query(tx):
            self._read(tx, graph)
            left, right = (
                ("<-", "-") if direction == "in" else ("-", "->" if direction == "out" else "-")
            )
            rows = tx.run(
                f"""MATCH path=(a:Entity {{program_id:$p,version_id:$v,entity_id:$id}})
                {left}[*1..{depth}]{right}(b:Entity)
                WHERE a<>b AND all(n IN nodes(path) WHERE n.program_id=$p AND n.version_id=$v)
                AND all(r IN relationships(path) WHERE r.program_id=$p AND r.version_id=$v
                    AND ($zero OR coalesce(r.matrix_value,1)<>0)
                    AND ($kind IS NULL OR r.type=$kind))
                WITH DISTINCT b ORDER BY b.entity_id RETURN b.payload AS payload""",
                p=graph["program_id"],
                v=graph["version_id"],
                id=entity_id,
                zero=include_zero,
                kind=relation_type,
            )
            items = [json.loads(record["payload"]) for record in rows]
            return {"total": len(items), "items": items[offset : offset + limit]}

        with self.driver.session(database=self.database) as session:
            return session.execute_read(query)

    @staticmethod
    @unit_of_work(timeout=30)
    def _read(tx, graph):
        scope = {"p": graph["program_id"], "v": graph["version_id"]}
        row = tx.run(
            "MATCH (m:ProgramVersionGraph {program_id:$p,version_id:$v}) RETURN properties(m) AS m",
            **scope,
        ).single()
        if row is None:
            raise DomainError(409, "Граф этой версии ещё не построен или требует восстановления")
        metadata = row["m"]
        expected = {key: value for key, value in graph.items() if key not in {"nodes", "edges"}}
        if not same_properties({key: metadata.get(key) for key in expected}, expected):
            raise DomainError(503, "Граф не соответствует сохранённой версии")
        nodes = [
            record.data()
            for record in tx.run(
                """MATCH (n:Entity {program_id:$p,version_id:$v})
            RETURN properties(n) AS props,labels(n) AS labels ORDER BY n.entity_id""",
                **scope,
            )
        ]
        edges = [
            record.data()
            for record in tx.run(
                """MATCH (a)-[r]->(b)
            WHERE (a:Entity AND a.program_id=$p AND a.version_id=$v)
                OR (b:Entity AND b.program_id=$p AND b.version_id=$v)
            RETURN properties(r) AS props,type(r) AS type,a.entity_id AS source_id,
                b.entity_id AS target_id,a.program_id AS ap,a.version_id AS av,
                b.program_id AS bp,b.version_id AS bv ORDER BY r.relation_id""",
                **scope,
            )
        ]
        if len(nodes) != graph["node_count"] or len(edges) != graph["relation_count"]:
            raise DomainError(503, "Нарушена целостность графа; требуется восстановление")
        for actual, original in zip(nodes, graph["nodes"], strict=True):
            if not same_properties(actual["props"], original["props"]) or set(actual["labels"]) != {
                "Entity",
                original["label"],
            }:
                raise DomainError(503, "Изменены сущности графа; требуется восстановление")
        for actual, original in zip(edges, graph["edges"], strict=True):
            if (
                any(
                    not same_properties(actual[key], original[key])
                    for key in ("props", "type", "source_id", "target_id")
                )
                or actual["ap"] != scope["p"]
                or actual["bp"] != scope["p"]
                or actual["av"] != scope["v"]
                or actual["bv"] != scope["v"]
            ):
                raise DomainError(503, "Изменены связи графа; требуется восстановление")
        return {
            "metadata": expected,
            "entities": [json.loads(item["props"]["payload"]) for item in nodes],
            "relations": [json.loads(item["props"]["payload"]) for item in edges],
        }
