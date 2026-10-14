"""FastAPI surface exposing the self-correcting Text-to-SQL engine.

Run locally:   uvicorn api.main:app --reload
In compose:    started by the `api` service, waits for a healthy database.

POST /api/query  { "question": "..." }
  -> status, final_sql, results, iterations, repair_history
GET  /health     -> liveness + database reachability + generator mode
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

load_dotenv()  # load .env if present, before importing config-sensitive code

from src.agent import FULL_PIPELINE, TextToSQLAgent  # noqa: E402
from src.db import Database  # noqa: E402
from src.llm import build_generator, is_offline  # noqa: E402

app = FastAPI(
    title="Self-Correcting Text-to-SQL Engine",
    version="1.0.0",
    description=(
        "Typed repair pipeline: sqlglot AST guardrails, schema validation, "
        "execution, semantic sanity heuristics, and bounded repair loops."
    ),
)

# One shared Database object; connections are lazily (re)opened and the catalog
# is cached. The agent itself is stateless per request.
_database = Database()


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, description="Natural-language question")


class RepairHistoryItem(BaseModel):
    attempt: int
    failure_type: str
    error_message: str


class QueryResponse(BaseModel):
    status: str
    final_sql: Optional[str] = None
    results: List[Dict[str, Any]] = Field(default_factory=list)
    iterations: int
    repair_history: List[RepairHistoryItem] = Field(default_factory=list)


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "database": _database.is_healthy(),
        "generator": "offline" if is_offline() else os.getenv("LLM_MODEL", "unknown"),
    }


@app.get("/")
def root() -> Dict[str, str]:
    return {"service": "text-to-sql-engine", "docs": "/docs", "query": "/api/query"}


@app.post("/api/query", response_model=QueryResponse)
def query(payload: QueryRequest) -> QueryResponse:
    agent = TextToSQLAgent(
        database=_database,
        generator=build_generator(),
        config=FULL_PIPELINE,
    )
    try:
        result = agent.run(payload.question)
    except Exception as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=503, detail=f"Pipeline error: {exc}") from exc
    return QueryResponse(**result.to_api_dict())
