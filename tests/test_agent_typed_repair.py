"""Orchestrator tests: guardrail routing, typed repairs, and the bounded loop.

These use a scripted fake generator and an in-memory fake database so the state
machine can be exercised deterministically without Postgres.
"""

from src.agent import (
    FAILURE_SCHEMA,
    FAILURE_SEMANTIC,
    FAILURE_SYNTAX,
    FULL_PIPELINE,
    ZERO_SHOT,
    TextToSQLAgent,
)
from src.db import QueryResult, SchemaError


class FakeGenerator:
    """Returns a scripted sequence of SQL strings; stays on the last one."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def generate_sql(self, question, schema_ddl, catalog, fks, history=None):
        self.calls += 1
        if self.script:
            return self.script.pop(0)
        return "SELECT 1"


class FakeDatabase:
    def __init__(self):
        self.executed = []

    def get_catalog(self):
        return {
            "customers": [
                {"name": "customer_id", "type": "integer", "nullable": False},
                {"name": "name", "type": "text", "nullable": False},
                {"name": "region", "type": "text", "nullable": True},
            ],
            "orders": [
                {"name": "id", "type": "integer", "nullable": False},
                {"name": "cust_id", "type": "integer", "nullable": False},
                {"name": "total_amount", "type": "numeric", "nullable": False},
            ],
        }

    def get_schema_ddl(self):
        return "TABLE customers (customer_id INTEGER, name TEXT, region TEXT);"

    def get_foreign_keys(self):
        return [
            {
                "child_table": "orders",
                "child_column": "cust_id",
                "parent_table": "customers",
                "parent_column": "customer_id",
            }
        ]

    def max_table_rows(self):
        return 600

    def execute(self, sql):
        self.executed.append(sql)
        if "nonexistent" in sql:
            raise SchemaError('column "nonexistent" does not exist')
        if "CROSSMARK" in sql:
            return QueryResult(columns=["id"], rows=[{"id": i} for i in range(72000)])
        if "'new york'" in sql:
            return QueryResult(columns=["name"], rows=[])
        return QueryResult(columns=["n"], rows=[{"n": 1}])


def test_guardrail_violation_is_logged_as_syntax_and_loop_is_bounded():
    gen = FakeGenerator(["DROP TABLE customers;"] * 5)
    db = FakeDatabase()
    agent = TextToSQLAgent(db, generator=gen, config=FULL_PIPELINE)

    result = agent.run("Delete everything")

    assert result.status == "failed"
    assert result.failure_class == FAILURE_SYNTAX
    assert result.iterations == 3  # hard cap
    assert all(h["failure_type"] == FAILURE_SYNTAX for h in result.repair_history)
    # The destructive statement must never reach the engine.
    assert not any("DROP" in s for s in db.executed)


def test_zero_shot_makes_exactly_one_attempt():
    gen = FakeGenerator(["DROP TABLE customers;"])
    agent = TextToSQLAgent(FakeDatabase(), generator=gen, config=ZERO_SHOT)
    result = agent.run("anything")
    assert result.iterations == 1
    assert result.status == "failed"


def test_schema_error_triggers_schema_repair_then_success():
    gen = FakeGenerator(
        [
            "SELECT nonexistent FROM customers",
            "SELECT name FROM customers",
        ]
    )
    db = FakeDatabase()
    agent = TextToSQLAgent(db, generator=gen, config=FULL_PIPELINE)

    result = agent.run("List customer names")

    assert result.status == "success"
    assert result.iterations == 2
    assert result.repair_history[0]["failure_type"] == FAILURE_SCHEMA


def test_semantic_empty_result_triggers_semantic_repair():
    gen = FakeGenerator(
        [
            "SELECT name FROM customers WHERE region = 'new york'",
            "SELECT name FROM customers WHERE region ILIKE 'New York'",
        ]
    )
    db = FakeDatabase()
    agent = TextToSQLAgent(db, generator=gen, config=FULL_PIPELINE)

    result = agent.run("Which customers are in New York?")

    assert result.status == "success"
    assert result.iterations == 2
    assert result.repair_history[0]["failure_type"] == FAILURE_SEMANTIC


def test_cartesian_product_is_flagged_semantically():
    gen = FakeGenerator(
        [
            "SELECT id FROM orders, customers /* CROSSMARK */",
            "SELECT id FROM orders",
        ]
    )
    db = FakeDatabase()
    agent = TextToSQLAgent(db, generator=gen, config=FULL_PIPELINE)

    result = agent.run("List orders")

    assert result.status == "success"
    assert result.repair_history[0]["failure_type"] == FAILURE_SEMANTIC
    assert result.repair_history[0]["heuristic"] == "cartesian_product"
