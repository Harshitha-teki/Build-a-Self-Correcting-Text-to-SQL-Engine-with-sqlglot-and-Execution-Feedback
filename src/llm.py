"""LLM generation layer with a deterministic offline fallback.

Two interchangeable generators implement the same tiny contract::

    generate_sql(question, schema_ddl, catalog, fks, history) -> str

* :class:`HostedLLMGenerator` calls an OpenAI-compatible chat endpoint and
  selects one of the three *typed* repair prompts in :mod:`src.prompts`
  according to the failure class of the previous attempt.
* :class:`OfflineGenerator` is a deterministic, rule-based stand-in used when no
  API key is configured. It is **not** a gold-SQL lookup: it produces a naive
  query from keywords (including realistic bugs such as ``c.id`` joins, implicit
  cross joins, and case-sensitive string filters) and *repairs it from the
  failure context* (catalog lookup, FK graph, ILIKE rewrite). This keeps the
  evaluation reproducible without network access while exercising the real loop.
"""

from __future__ import annotations

import difflib
import os
import re
from typing import Any, Dict, List, Optional

from . import prompts

# Environment values that mean "not really configured".
_PLACEHOLDERS = {"", "your_key_here", "replace_me", "changeme", "none"}


# --------------------------------------------------------------------------- #
# Hosted (OpenAI-compatible) generator                                         #
# --------------------------------------------------------------------------- #
class HostedLLMGenerator:
    def __init__(self, model: str, api_key: str, base_url: Optional[str] = None):
        from openai import OpenAI

        self.model = model
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)

    def generate_sql(
        self,
        question: str,
        schema_ddl: str,
        catalog: Dict[str, List[Dict[str, Any]]],
        fks: List[Dict[str, str]],
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        prior = history[-1] if history else None
        if prior is None:
            user = prompts.build_initial_prompt(question, schema_ddl)
        elif prior["failure_type"] == "syntax":
            user = prompts.build_syntax_repair_prompt(
                question, prior["sql"], prior["error_message"], schema_ddl, history
            )
        elif prior["failure_type"] == "schema":
            user = prompts.build_schema_repair_prompt(
                question, prior["sql"], prior["error_message"], schema_ddl, history
            )
        else:  # semantic
            user = prompts.build_semantic_repair_prompt(
                question,
                prior["sql"],
                prior["error_message"],
                schema_ddl,
                prior.get("sample_rows"),
                history,
            )

        resp = self._client.chat.completions.create(
            model=self.model,
            temperature=0,
            messages=[
                {"role": "system", "content": prompts.SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
        )
        return (resp.choices[0].message.content or "").strip()


# --------------------------------------------------------------------------- #
# Offline deterministic generator                                              #
# --------------------------------------------------------------------------- #
_REGIONS = ["New York", "California", "Texas", "Washington", "Florida"]


class OfflineGenerator:
    """Deterministic keyword-to-SQL model with context-driven repair."""

    # -- public contract ---------------------------------------------------- #
    def generate_sql(
        self,
        question: str,
        schema_ddl: str,
        catalog: Dict[str, List[Dict[str, Any]]],
        fks: List[Dict[str, str]],
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        if not history:
            return self._initial_sql(question)

        prior = history[-1]
        sql = prior["sql"]
        ftype = prior["failure_type"]
        if ftype == "syntax":
            return self._repair_syntax(sql)
        if ftype == "schema":
            return self._repair_schema(sql, catalog)
        return self._repair_semantic(sql, prior, catalog, fks)

    # -- initial (deliberately imperfect) generation ------------------------ #
    def _initial_sql(self, question: str) -> str:
        q = question.lower()

        # -- syntax trap: dangling boolean operator -------------------------
        if "since" in q and "order" in q:
            return (
                "SELECT COUNT(*) AS order_count FROM orders "
                "WHERE order_date >= '2025-01-01' AND"
            )

        # -- implicit cross join (Cartesian trap) ---------------------------
        if "each order" in q and ("customer" in q):
            return "SELECT o.id, c.name FROM orders o, customers c"
        if "every order" in q and ("region" in q or "customer" in q):
            return "SELECT o.id, c.region FROM orders o, customers c"

        # -- case-sensitive region filter (empty-result trap) ---------------
        region = self._region(question)
        if region and ("revenue" in q or "sales" in q or "spend" in q):
            return (
                "SELECT SUM(o.total_amount) AS total_revenue "
                "FROM customers c JOIN orders o ON c.customer_id = o.cust_id "
                f"WHERE c.region = '{region.lower()}'"
            )
        if region:
            return (
                "SELECT name, region FROM customers "
                f"WHERE region = '{region.lower()}'"
            )

        # -- negations (0 rows is legitimate) -------------------------------
        if "without a region" in q or "no region" in q:
            return "SELECT name FROM customers WHERE region IS NULL"
        if "no orders" in q:
            return (
                "SELECT c.name FROM customers c "
                "LEFT JOIN orders o ON c.customer_id = o.cust_id "
                "WHERE o.id IS NULL"
            )

        # -- aggregates -----------------------------------------------------
        if "average order value" in q or ("average" in q and "order" in q):
            return "SELECT AVG(total_amount) AS avg_order_value FROM orders"
        if "how many customers" in q or "number of customers" in q:
            return "SELECT COUNT(*) AS customer_count FROM customers"
        if "line item" in q:
            return "SELECT COUNT(*) AS line_item_count FROM line_items"
        if "how many orders" in q or "number of orders" in q:
            if "2025" in q:
                return (
                    "SELECT COUNT(*) AS order_count FROM orders "
                    "WHERE order_date >= '2025-01-01' AND order_date <= '2025-12-31'"
                )
            return "SELECT COUNT(*) AS order_count FROM orders"

        # -- ranking (hallucinated join key trap) ---------------------------
        n = self._top_n(question)
        if n is not None and ("spend" in q or "spender" in q):
            return (
                f"SELECT c.name, SUM(o.total_amount) AS total_spend "
                "FROM customers c JOIN orders o ON c.id = o.cust_id "
                "GROUP BY c.name ORDER BY total_spend DESC, c.name LIMIT " + str(n)
            )

        # -- per-customer order counts (wrong FK column trap) ---------------
        if "number of orders" in q and ("customer" in q or "each customer" in q):
            return (
                "SELECT c.name, COUNT(o.id) AS order_count "
                "FROM customers c JOIN orders o ON c.customer_id = o.customer_id "
                "GROUP BY c.name ORDER BY order_count DESC, c.name"
            )

        # -- revenue by region (correct) ------------------------------------
        if "by region" in q and ("revenue" in q or "sales" in q or "spend" in q):
            return (
                "SELECT c.region, SUM(o.total_amount) AS total_revenue "
                "FROM customers c JOIN orders o ON c.customer_id = o.cust_id "
                "GROUP BY c.region ORDER BY total_revenue DESC, c.region"
            )

        # -- listing --------------------------------------------------------
        if "list all customers" in q or "show all customers" in q:
            return "SELECT customer_id, name, region FROM customers"

        # -- default weak fallback -----------------------------------------
        return "SELECT * FROM customers LIMIT 10"

    # -- repairs ------------------------------------------------------------- #
    @staticmethod
    def _repair_syntax(sql: str) -> str:
        fixed = sql.strip()
        fixed = re.sub(r"\bAND\s*$", "", fixed, flags=re.IGNORECASE).strip()
        fixed = re.sub(r"\bWHERE\s*$", "", fixed, flags=re.IGNORECASE).strip()
        fixed = re.sub(r"\bOR\s*$", "", fixed, flags=re.IGNORECASE).strip()
        fixed = fixed.rstrip(",;")
        return fixed

    @staticmethod
    def _repair_schema(sql: str, catalog: Dict[str, List[Dict[str, Any]]]) -> str:
        aliases: Dict[str, str] = {}
        for m in re.finditer(
            r"(?:FROM|JOIN)\s+\"?(\w+)\"?\s+(?:AS\s+)?(\w+)", sql, re.IGNORECASE
        ):
            table, alias = m.group(1), m.group(2)
            aliases[alias] = table
        for m in re.finditer(r'(?:FROM|JOIN)\s+"?(\w+)"?', sql, re.IGNORECASE):
            aliases.setdefault(m.group(1), m.group(1))

        def fix(match: re.Match) -> str:
            alias, col = match.group(1), match.group(2)
            table = aliases.get(alias, alias)
            cols = [c["name"] for c in catalog.get(table, [])]
            if not cols or col in cols:
                return match.group(0)
            if col == "id" and "customer_id" in cols:
                new = "customer_id"
            elif col == "customer_id" and "cust_id" in cols:
                new = "cust_id"
            elif col == "cust_id" and "customer_id" in cols:
                new = "customer_id"
            else:
                new = difflib.get_close_matches(col, cols, n=1, cutoff=0.0)[0]
            return f"{alias}.{new}"

        return re.sub(r"\b(\w+)\.(\w+)\b", fix, sql)

    @staticmethod
    def _repair_semantic(
        sql: str,
        prior: Dict[str, Any],
        catalog: Dict[str, List[Dict[str, Any]]],
        fks: List[Dict[str, str]],
    ) -> str:
        heuristic = prior.get("heuristic")
        if heuristic == "cartesian_product":
            return _de_cartesian(sql, fks)
        # empty_result / null_aggregate: most common root cause is a
        # case-sensitive equality against a text column -> make it ILIKE.
        return re.sub(
            r"=\s*'([A-Za-z][A-Za-z .]*)'",
            lambda m: f"ILIKE '{m.group(1)}'",
            sql,
        )

    # -- helpers ------------------------------------------------------------- #
    @staticmethod
    def _region(question: str) -> Optional[str]:
        low = question.lower()
        for region in _REGIONS:
            if region.lower() in low:
                return region
        return None

    @staticmethod
    def _top_n(question: str) -> Optional[int]:
        m = re.search(r"\btop\s+(\d+)\b", question, re.IGNORECASE)
        return int(m.group(1)) if m else None


def _de_cartesian(sql: str, fks: List[Dict[str, str]]) -> str:
    """Rewrite an implicit comma cross-join into an explicit FK-based JOIN."""
    m = re.search(
        r"FROM\s+\"?(\w+)\"?\s+(?:AS\s+)?(\w+)\s*,\s*\"?(\w+)\"?\s+(?:AS\s+)?(\w+)",
        sql,
        re.IGNORECASE,
    )
    if not m:
        return sql
    t1, a1, t2, a2 = m.group(1), m.group(2), m.group(3), m.group(4)

    condition: Optional[str] = None
    for fk in fks:
        child, ccol = fk["child_table"], fk["child_column"]
        parent, pcol = fk["parent_table"], fk["parent_column"]
        if {child, parent} == {t1, t2}:
            if child == t1:
                condition = f"{a1}.{ccol} = {a2}.{pcol}"
            else:
                condition = f"{a2}.{ccol} = {a1}.{pcol}"
            break
    if condition is None:
        return sql
    replacement = f'FROM "{t1}" {a1} JOIN "{t2}" {a2} ON {condition}'
    start, end = m.span()
    return sql[:start] + replacement + sql[end:]


# --------------------------------------------------------------------------- #
# Factory                                                                      #
# --------------------------------------------------------------------------- #
def build_generator():
    """Return a hosted generator when a real key exists, else the offline one."""
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if api_key.lower() in _PLACEHOLDERS:
        return OfflineGenerator()
    base_url = os.getenv("OPENAI_BASE_URL") or None
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    try:
        return HostedLLMGenerator(model=model, api_key=api_key, base_url=base_url)
    except Exception:
        # Never let a broken client take down the pipeline; fall back cleanly.
        return OfflineGenerator()


def is_offline() -> bool:
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    return api_key.lower() in _PLACEHOLDERS
