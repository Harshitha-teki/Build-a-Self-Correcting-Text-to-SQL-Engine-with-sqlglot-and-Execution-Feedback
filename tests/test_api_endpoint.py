"""Contract tests for the FastAPI /api/query response schema."""

from fastapi.testclient import TestClient

import api.main as main
from src.db import QueryResult


class FakeGenerator:
    def generate_sql(self, question, schema_ddl, catalog, fks, history=None):
        return "SELECT customer_id, name, region FROM customers"


class FakeDatabase:
    def get_catalog(self):
        return {
            "customers": [
                {"name": "customer_id", "type": "integer", "nullable": False},
                {"name": "name", "type": "text", "nullable": False},
                {"name": "region", "type": "text", "nullable": True},
            ]
        }

    def get_schema_ddl(self):
        return "TABLE customers (customer_id INTEGER, name TEXT, region TEXT);"

    def get_foreign_keys(self):
        return []

    def max_table_rows(self):
        return 3

    def execute(self, sql):
        return QueryResult(
            columns=["customer_id", "name", "region"],
            rows=[
                {"customer_id": 1, "name": "Customer 1", "region": "New York"},
                {"customer_id": 2, "name": "Customer 2", "region": None},
            ],
        )

    def is_healthy(self):
        return True


def _client(monkeypatch):
    monkeypatch.setattr(main, "_database", FakeDatabase())
    monkeypatch.setattr(main, "build_generator", lambda: FakeGenerator())
    return TestClient(main.app)


def test_query_endpoint_returns_required_schema(monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/api/query", json={"question": "Show me all customers in New York"})
    assert resp.status_code == 200
    body = resp.json()
    for key in ("status", "final_sql", "results", "iterations", "repair_history"):
        assert key in body
    assert body["status"] == "success"
    assert body["iterations"] >= 1
    assert isinstance(body["results"], list)
    assert isinstance(body["repair_history"], list)


def test_health_endpoint(monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
