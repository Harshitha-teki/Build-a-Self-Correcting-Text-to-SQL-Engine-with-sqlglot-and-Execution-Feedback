"""Phase 3 :: Database connection, catalog introspection and execution wrapper.

The engine deliberately executes *raw SQL strings* (no ORM-generated Python).
This module owns three responsibilities:

* resolving connection settings from the environment / ``DATABASE_URL``;
* introspecting the catalog so the schema-repair prompt can show the LLM the
  real column names (``cust_id`` vs ``customer_id``);
* executing a query and *classifying* engine errors into a **schema** failure
  (undefined column / table / function) versus a generic **execution** failure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.errors as pg_errors
import psycopg2.extras


# --------------------------------------------------------------------------- #
# Exceptions                                                                  #
# --------------------------------------------------------------------------- #
class DatabaseError(Exception):
    """Base class for classified database failures."""

    kind = "execution"

    def __init__(self, message: str, *, sqlstate: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.sqlstate = sqlstate


class SchemaError(DatabaseError):
    """The query referenced a table/column/function that does not exist."""

    kind = "schema"


class ExecutionError(DatabaseError):
    """Any other engine-side failure (type errors, division by zero, ...)."""

    kind = "execution"


# SQLSTATE codes that mean "the object you named is not in the catalog".
_SCHEMA_SQLSTATES = {"42P01", "42703", "42883", "42704", "3F000"}
_SCHEMA_EXCEPTIONS = (
    pg_errors.UndefinedColumn,
    pg_errors.UndefinedTable,
    pg_errors.UndefinedFunction,
    pg_errors.UndefinedObject,
    pg_errors.UndefinedParameter,
)


@dataclass
class QueryResult:
    """A materialized result set with stable column ordering."""

    columns: List[str] = field(default_factory=list)
    rows: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def sample(self, n: int = 3) -> List[Dict[str, Any]]:
        return self.rows[:n]


def resolve_dsn() -> str:
    """Build a libpq DSN from ``DATABASE_URL`` or the individual DB_* vars."""
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    return (
        f"host={os.getenv('DB_HOST', 'localhost')} "
        f"port={os.getenv('DB_PORT', '5432')} "
        f"user={os.getenv('DB_USER', 'postgres')} "
        f"password={os.getenv('DB_PASSWORD', 'postgres')} "
        f"dbname={os.getenv('DB_NAME', 'ecommerce_messy')}"
    )


class Database:
    """Thin, safety-oriented wrapper around a psycopg2 connection."""

    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn or resolve_dsn()
        self._conn: Optional[psycopg2.extensions.connection] = None
        self._catalog_cache: Optional[Dict[str, List[Dict[str, Any]]]] = None
        self._row_counts_cache: Optional[Dict[str, int]] = None

    # -- connection lifecycle ------------------------------------------------ #
    def connect(self) -> psycopg2.extensions.connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self.dsn)
            self._conn.autocommit = True
            # Read-only session as defense-in-depth behind the AST guardrail.
            with self._conn.cursor() as cur:
                cur.execute("SET default_transaction_read_only = on")
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def is_healthy(self) -> bool:
        try:
            with self.connect().cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        except Exception:
            return False

    # -- catalog introspection ---------------------------------------------- #
    def get_catalog(self, refresh: bool = False) -> Dict[str, List[Dict[str, Any]]]:
        """Return ``{table: [{name, type, nullable}, ...]}`` for public tables."""
        if self._catalog_cache is not None and not refresh:
            return self._catalog_cache

        query = """
            SELECT table_name, column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
        """
        catalog: Dict[str, List[Dict[str, Any]]] = {}
        with self.connect().cursor() as cur:
            cur.execute(query)
            for table_name, column_name, data_type, is_nullable in cur.fetchall():
                catalog.setdefault(table_name, []).append(
                    {
                        "name": column_name,
                        "type": data_type,
                        "nullable": is_nullable == "YES",
                    }
                )
        self._catalog_cache = catalog
        return catalog

    def get_schema_ddl(self) -> str:
        """Render the catalog as a compact DDL string for repair prompts."""
        catalog = self.get_catalog()
        counts = self.get_table_row_counts()
        lines: List[str] = []
        for table in sorted(catalog):
            lines.append(f"TABLE {table} ({counts.get(table, 0)} rows) (")
            for col in catalog[table]:
                null = "" if col["nullable"] else " NOT NULL"
                lines.append(f"  {col['name']} {col['type'].upper()}{null},")
            lines.append(");")
        return "\n".join(lines)

    def get_foreign_keys(self) -> List[Dict[str, str]]:
        """Return FK edges as ``{child_table, child_column, parent_table, parent_column}``.

        Used by the semantic Cartesian-product repair to synthesize a correct
        JOIN condition from the actual catalog instead of guessing.
        """
        query = """
            SELECT
                tc.table_name       AS child_table,
                kcu.column_name     AS child_column,
                ccu.table_name      AS parent_table,
                ccu.column_name     AS parent_column
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.table_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
        """
        edges: List[Dict[str, str]] = []
        with self.connect().cursor() as cur:
            cur.execute(query)
            for child_t, child_c, parent_t, parent_c in cur.fetchall():
                edges.append(
                    {
                        "child_table": child_t,
                        "child_column": child_c,
                        "parent_table": parent_t,
                        "parent_column": parent_c,
                    }
                )
        return edges

    def get_table_row_counts(self, refresh: bool = False) -> Dict[str, int]:
        if self._row_counts_cache is not None and not refresh:
            return self._row_counts_cache
        counts: Dict[str, int] = {}
        for table in self.get_catalog():
            try:
                with self.connect().cursor() as cur:
                    cur.execute(f'SELECT COUNT(*) FROM "{table}"')
                    counts[table] = int(cur.fetchone()[0])
            except Exception:
                counts[table] = 0
        self._row_counts_cache = counts
        return counts

    def max_table_rows(self) -> int:
        counts = self.get_table_row_counts()
        return max(counts.values()) if counts else 0

    # -- execution ----------------------------------------------------------- #
    def execute(self, sql: str) -> QueryResult:
        """Execute a read-only query, returning rows as dictionaries.

        Raises:
            SchemaError: reference to a missing table/column/function.
            ExecutionError: any other engine-side failure.
        """
        try:
            with self.connect().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                rows = [dict(r) for r in cur.fetchall()] if cur.description else []
                columns = [d[0] for d in cur.description] if cur.description else []
                return QueryResult(columns=columns, rows=rows)
        except psycopg2.Error as exc:
            message = str(exc).strip()
            sqlstate = getattr(exc, "pgcode", None)
            if isinstance(exc, _SCHEMA_EXCEPTIONS) or sqlstate in _SCHEMA_SQLSTATES:
                raise SchemaError(message, sqlstate=sqlstate) from exc
            raise ExecutionError(message, sqlstate=sqlstate) from exc
