"""Phase 4 :: Semantic suspicion heuristics.

These checks run *after* a query has executed successfully. The database is
happy; the question is whether the *result* is plausible. This is the part a
naive "did it run?" pipeline skips, and it is where silently-wrong answers get
caught:

* **Cartesian product** -- the row count explodes far past anything the schema
  could justify, a tell-tale sign of a JOIN with a missing/incorrect ON clause.
* **Unexpectedly empty** -- a question that implies entities exist returns 0
  rows (usually an over-tight WHERE / off-by-one date filter).
* **Single NULL aggregate** -- a one-row aggregation (total, average, ...)
  comes back NULL, usually a broken filter or bad aggregation.

Returns are intentionally simple so the orchestrator can log a
``semantic`` failure together with a human-readable reason.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Default absolute floor for the Cartesian check (per contract).
DEFAULT_CARTESIAN_ROW_THRESHOLD = 1000
# When the catalog is available, also treat "10x the largest table" as a ceiling
# so a legitimately large listing is not punished on a bigger dataset.
CARTESIAN_MULTIPLIER = 10

# Questions using these terms imply a non-empty answer set.
_ENTITY_MARKERS = ("which", "who", "list", "show", "what", "find", "give", "name")

# ...unless they are explicitly negative / existential, where 0 rows is valid.
_NEGATIVE_MARKERS = (
    "are there any",
    "is there a",
    "is there any",
    "do any",
    "does any",
    "zero",
    "never",
    "without",
    "no customers",
    "no orders",
    "none",
)


def _expects_entities(question: str) -> bool:
    q = (question or "").lower()
    if any(marker in q for marker in _NEGATIVE_MARKERS):
        return False
    return any(marker in q for marker in _ENTITY_MARKERS)


def _cartesian_threshold(
    row_threshold: Optional[int], max_table_rows: Optional[int]
) -> int:
    # `row_threshold` is the absolute floor; the "largest table x10" rule is a
    # smarter ceiling that only applies once the catalog is known.
    floor = row_threshold if row_threshold is not None else DEFAULT_CARTESIAN_ROW_THRESHOLD
    if max_table_rows and max_table_rows > 0:
        return max(floor, CARTESIAN_MULTIPLIER * max_table_rows)
    return floor


def evaluate_semantic_sanity(
    sql: str,
    results: List[Dict[str, Any]],
    question: str,
    *,
    max_table_rows: Optional[int] = None,
    row_threshold: Optional[int] = None,
) -> Dict[str, Any]:
    """Evaluate an executed query for semantic suspicion.

    Params:
        sql (str): The executed SQL (kept for logging / future AST heuristics).
        results (list[dict]): Rows returned by the database.
        question (str): The original natural-language question.
        max_table_rows (int, optional): Largest table size, enables the
            "order of magnitude" Cartesian rule. Falls back to the 1000-row floor.
        row_threshold (int, optional): Explicit Cartesian threshold override.

    Returns:
        dict with keys ``is_suspicious`` (bool), ``reason`` (str | None) and
        ``heuristic`` (str | None) naming the rule that fired.

    Side effects: None.
    """
    results = results or []

    # Heuristic 1 :: Cartesian product / row-count explosion.
    threshold = _cartesian_threshold(row_threshold, max_table_rows)
    if len(results) > threshold:
        return {
            "is_suspicious": True,
            "reason": (
                f"Result set is unusually large ({len(results)} rows > {threshold} "
                "threshold). Check for missing JOIN conditions (Cartesian product)."
            ),
            "heuristic": "cartesian_product",
        }

    # Heuristic 2 :: Unexpectedly empty result for an entity-seeking question.
    if len(results) == 0 and _expects_entities(question):
        return {
            "is_suspicious": True,
            "reason": (
                "Query returned 0 rows, but the question implies entities exist. "
                "Check WHERE clause filters (date ranges, exact string matches, "
                "case sensitivity)."
            ),
            "heuristic": "empty_result",
        }

    # Heuristic 3 :: Single-row aggregation that came back fully NULL.
    if len(results) == 1:
        values = list(results[0].values())
        if values and all(v is None for v in values):
            return {
                "is_suspicious": True,
                "reason": (
                    "Aggregation returned a single NULL value. The WHERE clause or "
                    "the aggregated column is likely wrong (or the filter matched "
                    "no rows)."
                ),
                "heuristic": "null_aggregate",
            }

    return {"is_suspicious": False, "reason": None, "heuristic": None}
