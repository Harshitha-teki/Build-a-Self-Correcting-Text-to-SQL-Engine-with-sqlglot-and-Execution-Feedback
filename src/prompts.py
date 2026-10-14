"""Phase 5 :: Typed repair prompts.

The whole point of a *typed* repair policy is that a parse error, a schema
error and a semantic suspicion need different context. We therefore ship one
prompt builder per failure class rather than a single "fix this SQL" template.

* syntax   -> parser diagnostics + dialect rules
* schema   -> the real catalog DDL (column names / types)
* semantic -> the actual result (rows or empty set) + why it looks wrong
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

# The single most effective fix for "the model wrapped SQL in markdown" is an
# explicit, unambiguous instruction. We still keep the guardrail fence-stripper
# as a belt-and-braces fallback.
SYSTEM_PROMPT = (
    "You are a senior PostgreSQL analytics engineer. You translate a natural "
    "language question into exactly one read-only SELECT statement.\n"
    "RULES:\n"
    "1. Respond ONLY with valid PostgreSQL syntax. Do not include markdown "
    "formatting, backticks, code fences, or conversational text.\n"
    "2. Only SELECT statements are permitted. Never emit INSERT, UPDATE, "
    "DELETE, DROP, ALTER, TRUNCATE or DDL.\n"
    "3. Use only the tables and columns that appear in the provided schema. "
    "Do not invent identifiers.\n"
    "4. Prefer the denormalized orders.total_amount for order-level revenue "
    "questions; use line_items only when item-level detail is requested.\n"
    "5. When a question asks for a ranking ('top N'), include an ORDER BY and a "
    "LIMIT."
)


def _format_history(history: Optional[List[Dict[str, Any]]]) -> str:
    if not history:
        return ""
    lines = ["Previous failed attempts (do NOT repeat them):"]
    for item in history:
        lines.append(
            f"- Attempt {item.get('attempt')} [{item.get('failure_type')}]: "
            f"{(item.get('sql') or '').strip()[:400]}\n"
            f"  why it failed: {item.get('error_message')}"
        )
    return "\n".join(lines) + "\n\n"


def build_initial_prompt(question: str, schema_ddl: str) -> str:
    return (
        "### DATABASE SCHEMA\n"
        f"{schema_ddl}\n\n"
        "### QUESTION\n"
        f"{question}\n\n"
        "Write a single PostgreSQL SELECT query that answers the question. "
        "Respond with SQL only."
    )


def build_syntax_repair_prompt(
    question: str,
    bad_sql: str,
    error: str,
    schema_ddl: str,
    history: Optional[List[Dict[str, Any]]] = None,
) -> str:
    return (
        "### TASK: FIX A SYNTAX / SAFETY ERROR\n"
        "The previous query was rejected by the AST parser before it reached "
        "the database.\n\n"
        "### DATABASE SCHEMA\n"
        f"{schema_ddl}\n\n"
        "### ORIGINAL QUESTION\n"
        f"{question}\n\n"
        "### REJECTED SQL\n"
        f"{bad_sql}\n\n"
        "### PARSER DIAGNOSTIC\n"
        f"{error}\n\n"
        f"{_format_history(history)}"
        "Rewrite the query as valid PostgreSQL. Remember the dialect rules: "
        "quote string literals with single quotes, never use backticks, and "
        "return exactly one read-only SELECT statement with no trailing prose. "
        "Respond with SQL only."
    )


def build_schema_repair_prompt(
    question: str,
    bad_sql: str,
    error: str,
    schema_ddl: str,
    history: Optional[List[Dict[str, Any]]] = None,
) -> str:
    return (
        "### TASK: FIX A SCHEMA ERROR\n"
        "The query parsed cleanly but the database rejected it because a table "
        "or column does not exist. Use the catalog below to find the correct "
        "identifier.\n\n"
        "### DATABASE CATALOG (authoritative)\n"
        f"{schema_ddl}\n\n"
        "### ORIGINAL QUESTION\n"
        f"{question}\n\n"
        "### INVALID SQL\n"
        f"{bad_sql}\n\n"
        "### DATABASE ENGINE ERROR\n"
        f"{error}\n\n"
        f"{_format_history(history)}"
        "Replace every unknown identifier with one that exists in the catalog. "
        "Note the join mismatch: customers.customer_id <-> orders.cust_id. "
        "Respond with SQL only."
    )


def build_semantic_repair_prompt(
    question: str,
    bad_sql: str,
    reason: str,
    schema_ddl: str,
    sample_rows: Optional[List[Dict[str, Any]]] = None,
    history: Optional[List[Dict[str, Any]]] = None,
) -> str:
    sample = sample_rows if sample_rows is not None else []
    sample_text = json.dumps(sample[:3], default=str, indent=2) if sample else "(empty result set)"
    return (
        "### TASK: FIX A SEMANTICALLY SUSPICIOUS RESULT\n"
        "The query executed successfully, but the RESULT looks wrong for the "
        "question. This is a logic problem, not a syntax problem.\n\n"
        "### DATABASE SCHEMA\n"
        f"{schema_ddl}\n\n"
        "### ORIGINAL QUESTION\n"
        f"{question}\n\n"
        "### EXECUTED SQL (syntactically valid)\n"
        f"{bad_sql}\n\n"
        "### HEURISTIC SUSPICION\n"
        f"{reason}\n\n"
        "### ACTUAL RESULT (first 3 rows, or empty)\n"
        f"{sample_text}\n\n"
        f"{_format_history(history)}"
        "Diagnose the logic error (bad JOIN key, over-tight WHERE/date filter, "
        "wrong aggregation source) and rewrite the query. Respond with SQL only."
    )
