"""Phase 6 :: Stateful orchestrator with a *typed* repair policy.

The pipeline is a deterministic state machine, not an autonomous ReAct loop:

    Generate -> AST guardrail -> [syntax repair] -> execute
             -> [schema repair] -> semantic heuristics -> [semantic repair]
             -> Success | Failed (after MAX attempts)

Each failure class routes the LLM to a different repair prompt and every
attempt is recorded (attempt number + failure class + message) so a reviewer
can recompute what happened.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .db import Database, ExecutionError, SchemaError
from .guardrails import validate_and_guard_sql
from .heuristics import DEFAULT_CARTESIAN_ROW_THRESHOLD, evaluate_semantic_sanity
from .llm import build_generator

# Canonical failure classes exposed in the API / evaluation output.
FAILURE_SYNTAX = "syntax"
FAILURE_SCHEMA = "schema"
FAILURE_EXECUTION = "execution"
FAILURE_SEMANTIC = "semantic"


def _coerce(value: Any) -> Any:
    """Make DB values JSON-safe without losing reproducibility."""
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    return value


@dataclass
class PipelineConfig:
    """Toggles for the three evaluation configurations."""

    name: str
    max_attempts: int = 3
    repair_syntax: bool = True
    repair_schema: bool = True
    semantic_checks: bool = True


ZERO_SHOT = PipelineConfig(
    name="zero_shot",
    max_attempts=1,
    repair_syntax=False,
    repair_schema=False,
    semantic_checks=False,
)
SYNTAX_SCHEMA_REPAIR = PipelineConfig(
    name="syntax_schema_repair_only",
    max_attempts=3,
    repair_syntax=True,
    repair_schema=True,
    semantic_checks=False,
)
FULL_PIPELINE = PipelineConfig(
    name="full_pipeline",
    max_attempts=3,
    repair_syntax=True,
    repair_schema=True,
    semantic_checks=True,
)


@dataclass
class AgentResult:
    question: str
    status: str = "failed"
    final_sql: Optional[str] = None
    columns: List[str] = field(default_factory=list)
    results: List[Dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    repair_history: List[Dict[str, Any]] = field(default_factory=list)
    failure_class: Optional[str] = None
    generator: str = "unknown"

    def json_rows(self) -> List[Dict[str, Any]]:
        return [{k: _coerce(v) for k, v in row.items()} for row in self.results]

    def to_api_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "final_sql": self.final_sql,
            "results": self.json_rows(),
            "iterations": self.iterations,
            "repair_history": [
                {
                    "attempt": h["attempt"],
                    "failure_type": h["failure_type"],
                    "error_message": h["error_message"],
                }
                for h in self.repair_history
            ],
        }


class TextToSQLAgent:
    """Runs the generate/validate/execute/sanity/repair state machine."""

    def __init__(
        self,
        database: Database,
        generator=None,
        config: PipelineConfig = FULL_PIPELINE,
    ):
        self.db = database
        self.generator = generator or build_generator()
        self.config = config
        self.generator_name = type(self.generator).__name__

        env_threshold = os.getenv("CARTESIAN_ROW_THRESHOLD")
        self.row_threshold: Optional[int] = (
            int(env_threshold) if env_threshold else DEFAULT_CARTESIAN_ROW_THRESHOLD
        )
        env_sem = os.getenv("ENABLE_SEMANTIC_CHECKS", "true").lower()
        if env_sem in {"0", "false", "no"}:
            self.config.semantic_checks = False

    # ------------------------------------------------------------------ #
    def run(self, question: str) -> AgentResult:
        cfg = self.config
        result = AgentResult(question=question, generator=self.generator_name)

        # Snapshot the catalog once so every repair prompt sees the same truth.
        catalog = self.db.get_catalog()
        schema_ddl = self.db.get_schema_ddl()
        try:
            fks = self.db.get_foreign_keys()
        except Exception:
            fks = []
        max_table_rows = self.db.max_table_rows()

        history: List[Dict[str, Any]] = []
        sql = self.generator.generate_sql(question, schema_ddl, catalog, fks, history)

        last_qr = None  # last successful execution (kept even if we exit "failed")

        for attempt in range(1, cfg.max_attempts + 1):
            result.iterations = attempt

            # ---- Gate 1: AST guardrail + syntax -------------------------- #
            guard = validate_and_guard_sql(sql)
            if not guard["is_valid"]:
                entry = self._record(
                    history,
                    attempt=attempt,
                    failure_type=FAILURE_SYNTAX,
                    error_message=guard["error"],
                    sql=sql,
                )
                result.repair_history.append(entry)
                if not cfg.repair_syntax or attempt == cfg.max_attempts:
                    result.status = "failed"
                    result.failure_class = FAILURE_SYNTAX
                    result.final_sql = sql
                    self._attach_last(result, last_qr)
                    return result
                sql = self.generator.generate_sql(
                    question, schema_ddl, catalog, fks, history
                )
                continue

            sql = guard["sql"]

            # ---- Gate 2: schema validation via execution ---------------- #
            try:
                qr = self.db.execute(sql)
            except SchemaError as exc:
                entry = self._record(
                    history,
                    attempt=attempt,
                    failure_type=FAILURE_SCHEMA,
                    error_message=exc.message,
                    sql=sql,
                )
                result.repair_history.append(entry)
                if not cfg.repair_schema or attempt == cfg.max_attempts:
                    result.status = "failed"
                    result.failure_class = FAILURE_SCHEMA
                    result.final_sql = sql
                    self._attach_last(result, last_qr)
                    return result
                sql = self.generator.generate_sql(
                    question, schema_ddl, catalog, fks, history
                )
                continue
            except ExecutionError as exc:
                entry = self._record(
                    history,
                    attempt=attempt,
                    failure_type=FAILURE_EXECUTION,
                    error_message=exc.message,
                    sql=sql,
                )
                result.repair_history.append(entry)
                if not cfg.repair_schema or attempt == cfg.max_attempts:
                    result.status = "failed"
                    result.failure_class = FAILURE_EXECUTION
                    result.final_sql = sql
                    self._attach_last(result, last_qr)
                    return result
                sql = self.generator.generate_sql(
                    question, schema_ddl, catalog, fks, history
                )
                continue

            last_qr = qr

            # ---- Gate 3: semantic sanity heuristics --------------------- #
            if cfg.semantic_checks:
                verdict = evaluate_semantic_sanity(
                    sql,
                    qr.rows,
                    question,
                    max_table_rows=max_table_rows,
                    row_threshold=self.row_threshold,
                )
                if verdict["is_suspicious"]:
                    entry = self._record(
                        history,
                        attempt=attempt,
                        failure_type=FAILURE_SEMANTIC,
                        error_message=verdict["reason"],
                        sql=sql,
                        heuristic=verdict["heuristic"],
                        sample_rows=qr.sample(3),
                    )
                    result.repair_history.append(entry)
                    if attempt == cfg.max_attempts:
                        result.status = "failed"
                        result.failure_class = FAILURE_SEMANTIC
                        result.final_sql = sql
                        self._attach_last(result, last_qr)
                        return result
                    sql = self.generator.generate_sql(
                        question, schema_ddl, catalog, fks, history
                    )
                    continue

            # ---- Success ------------------------------------------------- #
            result.status = "success"
            result.final_sql = sql
            result.columns = qr.columns
            result.results = qr.rows
            result.failure_class = None
            return result

        # Defensive fallback (should be unreachable).
        result.status = "failed"
        result.final_sql = sql
        self._attach_last(result, last_qr)
        return result

    # ------------------------------------------------------------------ #
    @staticmethod
    def _record(
        history: List[Dict[str, Any]],
        *,
        attempt: int,
        failure_type: str,
        error_message: str,
        sql: str,
        heuristic: Optional[str] = None,
        sample_rows: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        entry = {
            "attempt": attempt,
            "failure_type": failure_type,
            "error_message": error_message,
            "sql": sql,
            "heuristic": heuristic,
            "sample_rows": sample_rows,
        }
        history.append(entry)
        return entry

    @staticmethod
    def _attach_last(result: AgentResult, qr) -> None:
        if qr is not None:
            result.columns = qr.columns
            result.results = qr.rows


def default_agent(config: PipelineConfig = FULL_PIPELINE) -> TextToSQLAgent:
    """Build an agent backed by the environment-configured database."""
    return TextToSQLAgent(Database(), config=config)
