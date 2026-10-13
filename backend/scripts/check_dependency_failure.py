"""Проверка намеренно остановленной зависимости в отдельном тестовом окружении."""

import sys

import httpx


def main():
    dependency = sys.argv[1]
    with httpx.Client(base_url="http://api:8000", timeout=12, trust_env=False) as http:
        response = http.get("/health/ready")
        assert response.status_code == 503, response.text
        states = response.json()["dependencies"]
        assert states[dependency] == "unavailable"
        assert set(states) == {"postgres", "temporal", "chroma"}
        if dependency == "postgres":
            # Temporal использует тот же PostgreSQL и тоже может стать недоступным.
            assert states["chroma"] == "available"
        else:
            assert all(value == "available" for key, value in states.items() if key != dependency)
        assert http.get("/health/live").status_code == 200
        if dependency == "postgres":
            response = http.get("/api/v1/programs")
            assert response.status_code == 503, response.text
            assert response.json() == {"detail": "Storage temporarily unavailable"}
        print("Dependency failure passed:", dependency, "readiness=503 liveness=200")


if __name__ == "__main__":
    main()
