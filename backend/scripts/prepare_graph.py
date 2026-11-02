"""Идемпотентная установка ограничений Neo4j перед запуском API и Worker."""

from app.config import get_settings
from app.graph_store import GraphStore


def main():
    store = GraphStore(get_settings())
    try:
        store.initialize()
        store.ping()
        print("Ограничения графа установлены, соединение проверено")
    finally:
        store.close()


if __name__ == "__main__":
    main()
