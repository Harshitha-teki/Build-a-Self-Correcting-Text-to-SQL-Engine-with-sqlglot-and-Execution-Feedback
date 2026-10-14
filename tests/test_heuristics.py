"""Unit tests for the semantic suspicion heuristics (Phase 4)."""

from src.heuristics import evaluate_semantic_sanity


def _rows(n):
    return [{"id": i} for i in range(n)]


def test_cartesian_product_flagged_above_threshold():
    verdict = evaluate_semantic_sanity(
        "SELECT * FROM orders, customers",
        _rows(1500),
        "List each order with its customer",
    )
    assert verdict["is_suspicious"] is True
    assert verdict["heuristic"] == "cartesian_product"


def test_small_result_not_flagged():
    verdict = evaluate_semantic_sanity("SELECT id FROM orders", _rows(10), "List orders")
    assert verdict["is_suspicious"] is False


def test_catalog_aware_threshold_allows_large_listing():
    # 1800 rows is a legitimate listing when the biggest table has 1800 rows.
    verdict = evaluate_semantic_sanity(
        "SELECT * FROM line_items",
        _rows(1800),
        "Show all line items",
        max_table_rows=1800,
    )
    assert verdict["is_suspicious"] is False


def test_empty_result_for_entity_question_flagged():
    verdict = evaluate_semantic_sanity(
        "SELECT name FROM customers WHERE region = 'new york'",
        [],
        "Who are the customers in New York?",
    )
    assert verdict["is_suspicious"] is True
    assert verdict["heuristic"] == "empty_result"


def test_empty_result_for_negative_question_not_flagged():
    verdict = evaluate_semantic_sanity(
        "SELECT c.name FROM customers c LEFT JOIN orders o ON c.customer_id = o.cust_id WHERE o.id IS NULL",
        [],
        "Are there any customers with no orders?",
    )
    assert verdict["is_suspicious"] is False


def test_single_null_aggregate_flagged():
    verdict = evaluate_semantic_sanity(
        "SELECT SUM(total_amount) AS total FROM orders WHERE 1 = 0",
        [{"total_revenue": None}],
        "What is the total revenue?",
    )
    assert verdict["is_suspicious"] is True
    assert verdict["heuristic"] == "null_aggregate"


def test_normal_aggregate_not_flagged():
    verdict = evaluate_semantic_sanity(
        "SELECT SUM(total_amount) AS total FROM orders",
        [{"total_revenue": 12345.67}],
        "What is the total revenue?",
    )
    assert verdict["is_suspicious"] is False
