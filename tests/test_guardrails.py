"""Unit tests for the sqlglot AST guardrails (Phase 2)."""

from src.guardrails import strip_markdown, validate_and_guard_sql


def test_valid_select_is_allowed():
    result = validate_and_guard_sql("SELECT id, name FROM customers")
    assert result["is_valid"] is True
    assert result["error"] is None
    assert "customers" in result["sql"]


def test_markdown_fences_are_stripped():
    raw = "```sql\nSELECT COUNT(*) FROM orders\n```"
    result = validate_and_guard_sql(raw)
    assert result["is_valid"] is True
    assert "```" not in result["sql"]


def test_markdown_strip_prose():
    raw = "Sure! Here is the query:\n```sql\nSELECT 1;\n```\nHope it helps."
    assert strip_markdown(raw).lower().startswith("select")


def test_cte_select_is_allowed():
    sql = "WITH x AS (SELECT 1 AS a) SELECT a FROM x"
    result = validate_and_guard_sql(sql)
    assert result["is_valid"] is True


def test_union_select_is_allowed():
    sql = "SELECT id FROM orders UNION SELECT customer_id FROM customers"
    assert validate_and_guard_sql(sql)["is_valid"] is True


def test_drop_is_rejected_as_syntax_guardrail():
    result = validate_and_guard_sql("DROP TABLE customers;")
    assert result["is_valid"] is False
    assert result["failure_type"] == "syntax"
    assert "Guardrail" in result["error"] or "SELECT" in result["error"]


def test_delete_is_rejected():
    result = validate_and_guard_sql("DELETE FROM orders")
    assert result["is_valid"] is False
    assert result["failure_type"] == "syntax"


def test_update_is_rejected():
    assert validate_and_guard_sql("UPDATE orders SET total_amount = 0")["is_valid"] is False


def test_insert_is_rejected():
    assert validate_and_guard_sql("INSERT INTO customers (name) VALUES ('x')")["is_valid"] is False


def test_truncate_is_rejected():
    assert validate_and_guard_sql("TRUNCATE TABLE line_items")["is_valid"] is False


def test_alter_is_rejected():
    assert validate_and_guard_sql("ALTER TABLE customers ADD COLUMN x INT")["is_valid"] is False


def test_multiple_statements_are_rejected():
    sql = "SELECT 1; DROP TABLE customers;"
    result = validate_and_guard_sql(sql)
    assert result["is_valid"] is False


def test_syntax_error_is_flagged_as_syntax():
    result = validate_and_guard_sql("SELECT FROM WHERE")
    assert result["is_valid"] is False
    assert result["failure_type"] == "syntax"


def test_empty_string_is_rejected():
    assert validate_and_guard_sql("")["is_valid"] is False
