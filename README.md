# Build-a-Self-Correcting-Text-to-SQL-Engine-with-sqlglot-and-Execution-Feedback

A typed-repair Text-to-SQL agent that parses SQL with `sqlglot`, validates against the database catalog, executes read-only queries, applies semantic sanity checks, and performs class-specific self-correction. The loop is deterministic and bounded (max 3 attempts) with explicit failure classification (`syntax`, `schema`, `semantic`, `execution`).

## Architecture (Mermaid)

```mermaid
flowchart TD
  A[Natural Language Question] --> B[LLM: Generate Initial SQL]
  B --> C[sqlglot: AST Guardrails + Syntax Check]
  C -- "Syntax Error/Unsafe" --> D[Syntax Repair Prompt]
  D -- "Retry < 3" --> B
  C -- "Valid SELECT" --> E[Database: Catalog Validation + Execute]
  E -- "Schema/Execution Error" --> F[Schema/Exec Repair Prompt]
  F -- "Retry < 3" --> B
  E -- "Execution Success" --> G[Heuristics: Semantic Sanity Check]
  G -- "Suspicious Results" --> H[Semantic Repair Prompt]
  H -- "Retry < 3" --> B
  G -- "Passed Checks" --> I[Success: Final SQL + Data]
  D -- "Max Retries Exceeded" --> J[Failed]
  F -- "Max Retries Exceeded" --> J[Failed]
  H -- "Max Retries Exceeded" --> J[Failed]
```

## Quickstart (Reproducible via Docker Compose)

> Everything is pinned (deps, Postgres 16) and designed to run offline; if no `OPENAI_API_KEY` is set, a deterministic rule-based generator runs to keep the evaluation reproducible.

```bash
# 1) Start DB + API (healthchecks enforced)
docker compose up -d --build

# 2) Verify health
curl http://localhost:8000/health  # {"status":"ok","database":true,"generator":"offline"|"gpt-4o-mini"}

# 3) Query the engine
curl -X POST http://localhost:8000/api/query \
  -H "Content-Type: application/json" \
  -d '{"question":"Who are the top 5 spenders?"}' | jq .
```

The API returns the contract schema: `status`, `final_sql`, `results`, `iterations`, `repair_history` (each with `attempt`, `failure_type`, `error_message`).

## Database (Intentional Messiness)

Seeded via `scripts/seed_db.sql` (auto-loaded on first Postgres boot):

| Table | Columns | Messy Constraints |
|---|---|---|
| `customers` | `customer_id`, `name`, `region` | `customer_id` is PK (not `id`). `region` is NULLable. |
| `orders` | `id`, `cust_id`, `total_amount`, `order_date` | `cust_id` (FK to `customer_id`) creates join ambiguity. `total_amount` is denormalized and intentionally diverges from `SUM(qty*unit_price)` every 5th order. |
| `line_items` | `id`, `order_id`, `qty`, `unit_price`, `product` | Granular data to trap bad aggregations/Cartesian products. |

Deterministic seed produces: **120 customers**, **600 orders**, **1800 line_items** (sufficient to trigger Cartesian row explosions).

## Core Components

| Module | Purpose |
|---|---|
| `src/guardrails.py` | `sqlglot` parse + AST traversal: rejects anything not a read-only `SELECT`/`WITH SELECT`/`UNION`. Strips markdown fences. Logs `syntax` failures. |
| `src/db.py` | Raw SQL execution (psycopg2), catalog introspection (`get_catalog`, `get_schema_ddl`, `get_foreign_keys`), schema/error classification (`SchemaError` vs `ExecutionError`). |
| `src/heuristics.py` | Semantic checks: Cartesian product (`row_count > threshold` or `> 10*max_table_rows`), empty result on entity-seeking questions, single NULL aggregate. |
| `src/prompts.py` | Three typed repair prompt builders (syntax/schema/semantic) + system instructions enforcing "SQL only, no markdown". |
| `src/llm.py` | Hosted (OpenAI-compatible) generator + deterministic `OfflineGenerator` fallback (repairs from failure context without network). |
| `src/agent.py` | Stateful orchestrator with bounded loop (max 3). Records full `repair_history` per attempt. Exposes `PipelineConfig`s (`zero_shot`, `syntax_schema_repair_only`, `full_pipeline`). |
| `api/main.py` | FastAPI `POST /api/query` and `GET /health`. |
| `scripts/evaluate.py` | Batch evaluation of all three configs. Execution-accuracy = result sets match (order-insensitive). Writes `results/evaluation_metrics.json`. |
| `scripts/seed_db.sql` | Idempotent, deterministic schema + data + self-check for volume. |

## Evaluation

The batch script runs `data/eval_set.json` (≥30 questions with `gold_sql`) against three configurations:

```json
{
  "configurations": {
    "zero_shot": {
      "accuracy_pct": "float",
      "avg_iterations": "float"
    },
    "syntax_schema_repair_only": {
      "accuracy_pct": "float",
      "avg_iterations": "float",
      "total_schema_repairs": "integer"
    },
    "full_pipeline": {
      "accuracy_pct": "float",
      "avg_iterations": "float",
      "total_semantic_repairs": "integer"
    }
  }
}
```

Run the evaluation:

```bash
# From host with local Python + DB up
python scripts/evaluate.py

# From inside compose container
docker compose exec -T api python scripts/evaluate.py
```

## Environment Variables

See `.env.example`. Key values:

- `OPENAI_API_KEY` (optional). If left as placeholder, the system uses offline deterministic generator.
- `LLM_MODEL` (default `gpt-4o-mini`)
- `DB_*` / `DATABASE_URL` (default `postgresql://postgres:postgres@localhost:5432/ecommerce_messy`; inside compose host is `db`)
- `MAX_REPAIR_ATTEMPTS` (default 3)
- `CARTESIAN_ROW_THRESHOLD` (default 1000; dynamic via catalog max table rows)
- `ENABLE_SEMANTIC_CHECKS` (default `true`)

## Tests

```bash
# Unit + integration contract tests
pytest -q  # 28 passed

# Run inside container
docker compose exec -T api python -m pytest tests -q
```

## Notes on the Offline Generator

The offline fallback is deliberately imperfect (e.g., guesses `c.id` for the FK, uses case-sensitive string equality, emits comma cross-joins) to force the *typed repair loop* to fire. It fixes errors using actual catalog/FKs and failure context (schema lookup + FK graph + ILIKE rewrite). It is **not** a gold lookup — it demonstrates the mechanism works deterministically without external LLM calls.

## Example Failure Classes

| Scenario | Failure Type | Repair Action |
|---|---|---|
| Markdown fences, malformed SQL | `syntax` | Parser diagnostics + dialect rules |
| `c.id` vs `cust_id` / missing column | `schema` | Real catalog DDL supplied; nearest-column correction |
| Comma join producing 72k rows | `semantic` (cartesian_product) | FK-based explicit JOIN rewrite |
| Region match returns 0 (case) | `semantic` (empty_result) | ILIKE transformation + filter advice |

## License

MIT
