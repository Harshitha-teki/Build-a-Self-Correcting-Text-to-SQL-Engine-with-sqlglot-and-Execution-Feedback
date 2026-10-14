"""Phase 6 :: Batch evaluation over three pipeline configurations.

Runs every question in ``data/eval_set.json`` through:

1. zero_shot                  -- generate + execute once, no repair.
2. syntax_schema_repair_only  -- repair parse/schema errors, ignore heuristics.
3. full_pipeline              -- syntax + schema + semantic repairs.

Correctness = execution accuracy: the generated result set must match the
gold-standard result set (order-insensitive). Metrics are written to
``results/evaluation_metrics.json`` with the exact keys the contract requires,
plus an audit block (per-question traces, failure-class breakdown, pinned ids).

Usage:
    python scripts/evaluate.py
"""

from __future__ import annotations

import datetime as _dt
import decimal
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# Make the repo root importable when invoked as `python scripts/evaluate.py`.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from src.agent import (  # noqa: E402
    FULL_PIPELINE,
    SYNTAX_SCHEMA_REPAIR,
    ZERO_SHOT,
    PipelineConfig,
    TextToSQLAgent,
)
from src.db import Database  # noqa: E402
from src.llm import build_generator, is_offline  # noqa: E402

EVAL_SET = ROOT / "data" / "eval_set.json"
OUT_PATH = ROOT / "results" / "evaluation_metrics.json"


# --------------------------------------------------------------------------- #
# Result-set comparison                                                       #
# --------------------------------------------------------------------------- #
def _normalize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, decimal.Decimal)):
        return round(float(value), 4)
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    return str(value)


def _canonical(rows: List[Dict[str, Any]]) -> List[Any]:
    canonical = [tuple(_normalize(v) for v in row.values()) for row in rows]
    canonical.sort(key=repr)
    return canonical


def results_match(
    generated: List[Dict[str, Any]], gold: List[Dict[str, Any]]
) -> bool:
    return _canonical(generated) == _canonical(gold)


# --------------------------------------------------------------------------- #
# Evaluation driver                                                           #
# --------------------------------------------------------------------------- #
def run_configuration(
    name: str,
    config: PipelineConfig,
    questions: List[Dict[str, Any]],
    gold_rows: Dict[int, List[Dict[str, Any]]],
    database: Database,
) -> Dict[str, Any]:
    generator = build_generator()
    agent = TextToSQLAgent(database=database, generator=generator, config=config)

    correct = 0
    iterations_total = 0
    failure_counts: Dict[str, int] = {}
    per_question: List[Dict[str, Any]] = []

    for item in questions:
        qid = item["id"]
        try:
            result = agent.run(item["question"])
        except Exception as exc:  # never let one bad question abort the batch
            per_question.append(
                {
                    "id": qid,
                    "status": "error",
                    "iterations": 0,
                    "correct": False,
                    "error": str(exc),
                }
            )
            continue

        matched = results_match(result.results, gold_rows[qid])
        correct += int(matched)
        iterations_total += result.iterations

        for entry in result.repair_history:
            failure_counts[entry["failure_type"]] = (
                failure_counts.get(entry["failure_type"], 0) + 1
            )

        per_question.append(
            {
                "id": qid,
                "difficulty": item.get("difficulty"),
                "status": result.status,
                "iterations": result.iterations,
                "correct": matched,
                "final_sql": result.final_sql,
                "failure_class": result.failure_class,
                "repair_history": result.repair_history,
                "row_count": len(result.results),
            }
        )

    n = len(questions)
    block: Dict[str, Any] = {
        "accuracy_pct": round(100.0 * correct / n, 2) if n else 0.0,
        "avg_iterations": round(iterations_total / n, 3) if n else 0.0,
    }
    # Contract-specific counters.
    if name == "syntax_schema_repair_only":
        block["total_schema_repairs"] = failure_counts.get("schema", 0)
    if name == "full_pipeline":
        block["total_semantic_repairs"] = failure_counts.get("semantic", 0)

    block["total_syntax_repairs"] = failure_counts.get("syntax", 0)
    block["failure_class_breakdown"] = failure_counts
    block["per_question"] = per_question
    return block


def main() -> int:
    questions = json.loads(EVAL_SET.read_text(encoding="utf-8"))
    if len(questions) < 30:
        print(f"WARNING: eval set has only {len(questions)} questions", file=sys.stderr)

    database = Database()
    if not database.is_healthy():
        print(
            "ERROR: cannot reach the database. Start it with `docker compose up -d db` "
            "and ensure DB_* / DATABASE_URL are set.",
            file=sys.stderr,
        )
        return 2

    # Execute gold SQL once; cache for all configurations.
    gold_rows: Dict[int, List[Dict[str, Any]]] = {}
    for item in questions:
        try:
            gold_rows[item["id"]] = database.execute(item["gold_sql"]).rows
        except Exception as exc:
            print(f"WARNING: gold SQL failed for q{item['id']}: {exc}", file=sys.stderr)
            gold_rows[item["id"]] = []

    configurations = {
        "zero_shot": run_configuration(
            "zero_shot", ZERO_SHOT, questions, gold_rows, database
        ),
        "syntax_schema_repair_only": run_configuration(
            "syntax_schema_repair_only",
            SYNTAX_SCHEMA_REPAIR,
            questions,
            gold_rows,
            database,
        ),
        "full_pipeline": run_configuration(
            "full_pipeline", FULL_PIPELINE, questions, gold_rows, database
        ),
    }

    output: Dict[str, Any] = {
        "configurations": configurations,
        "metadata": {
            "dataset": str(EVAL_SET.relative_to(ROOT)),
            "num_questions": len(questions),
            "generator": "offline_rule_based" if is_offline() else os.getenv("LLM_MODEL"),
            "llm_model_id": os.getenv("LLM_MODEL", "offline"),
            "eval_set_version": os.getenv("EVAL_SET_VERSION", "unversioned"),
            "max_attempts": FULL_PIPELINE.max_attempts,
            "cartesian_row_threshold": os.getenv(
                "CARTESIAN_ROW_THRESHOLD", "dynamic(max(1000, 10*max_table_rows))"
            ),
        },
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")

    # Console summary.
    print("\n=== Evaluation summary ===")
    for name, block in configurations.items():
        print(
            f"{name:28s} accuracy={block['accuracy_pct']:6.2f}%  "
            f"avg_iterations={block['avg_iterations']:.3f}  "
            f"failures={block['failure_class_breakdown']}"
        )
    print(f"\nWrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
