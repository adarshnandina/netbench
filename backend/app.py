"""NetBench backend: collects benchmark runs from all clients in one PostgreSQL database.

Endpoints
  GET  /health            is the API up and can it reach the database?   (no key needed)
  POST /runs              store one run record (the JSON the client saves)
  GET  /runs              list runs, newest first, optionally for one network
  GET  /runs/{run_id}     one full run, including raw samples
  GET  /results           flat table of metrics (one row per test per run) for analysis

Every endpoint except /health needs the header  X-API-Key: <NETBENCH_API_KEY>.
Interactive docs: http://localhost:8000/docs
"""
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

DATABASE_URL = os.environ["DATABASE_URL"]
API_KEY = os.environ["NETBENCH_API_KEY"]
SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()


def jsonb(value):
    """Wrap for a JSONB column, but keep Python None as SQL NULL (not the JSON value 'null')."""
    return None if value is None else Jsonb(value)


def connect():
    # One short-lived connection per request: simple, and plenty for a few
    # clients. A busy service would use a connection pool instead.
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


@asynccontextmanager
async def lifespan(app):
    with connect() as conn:
        conn.execute(SCHEMA_SQL)  # create tables on first start
    yield


app = FastAPI(title="NetBench API", lifespan=lifespan)


# ---------- request validation ----------
# Pydantic rejects malformed uploads with a 422 before they reach the database.

class TestResult(BaseModel):
    test: Literal["cyclic_udp", "reqresp_tcp", "bulk_tcp"]
    passed: bool
    params: dict
    thresholds: dict | None = None
    metrics: dict | None = None
    failed_checks: list[str] | None = None
    samples_ms: list[float | None] | None = None
    error: str | None = None


class Run(BaseModel):
    schema_version: Literal[1]
    run_id: UUID
    started_at: datetime
    network: str = Field(min_length=1, max_length=64)
    server: str
    client: dict
    tests: list[TestResult] = Field(min_length=1)


def require_api_key(x_api_key: str = Header()):
    # compare_digest takes the same time whether the first or last character
    # is wrong, so an attacker can't guess the key one character at a time.
    if not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="invalid API key")


# ---------- endpoints ----------

@app.get("/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/runs", status_code=201, dependencies=[Depends(require_api_key)])
def create_run(run: Run, response: Response):
    # `with connect()` is one transaction: the run and all its tests are
    # stored together, or (on any error) nothing is stored.
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO runs (run_id, started_at, network, server, client, schema_version)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (run_id) DO NOTHING""",
            (run.run_id, run.started_at, run.network, run.server, Jsonb(run.client), run.schema_version),
        )
        if cur.rowcount == 0:
            # Same run_id uploaded again (e.g. a retry after a timeout): not an error.
            response.status_code = 200
            return {"run_id": run.run_id, "status": "already stored"}

        for t in run.tests:
            conn.execute(
                """INSERT INTO test_results
                   (run_id, test, passed, error, params, thresholds, metrics, failed_checks, samples_ms)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (run.run_id, t.test, t.passed, t.error, jsonb(t.params),
                 jsonb(t.thresholds), jsonb(t.metrics), jsonb(t.failed_checks), jsonb(t.samples_ms)),
            )
    return {"run_id": run.run_id, "status": "stored"}


@app.get("/runs", dependencies=[Depends(require_api_key)])
def list_runs(network: str | None = None, limit: int = Query(50, ge=1, le=1000)):
    with connect() as conn:
        return conn.execute(
            """SELECT r.run_id, r.started_at, r.network, r.server,
                      bool_and(t.passed)                AS all_passed,
                      jsonb_object_agg(t.test, t.passed) AS passed
               FROM runs r JOIN test_results t USING (run_id)
               WHERE %(network)s::text IS NULL OR r.network = %(network)s
               GROUP BY r.run_id
               ORDER BY r.started_at DESC
               LIMIT %(limit)s""",
            {"network": network, "limit": limit},
        ).fetchall()


@app.get("/runs/{run_id}", dependencies=[Depends(require_api_key)])
def get_run(run_id: UUID):
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE run_id = %s", (run_id,)).fetchone()
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        run["tests"] = conn.execute(
            "SELECT * FROM test_results WHERE run_id = %s ORDER BY test", (run_id,)
        ).fetchall()
    return run


@app.get("/results", dependencies=[Depends(require_api_key)])
def list_results(network: str | None = None, test: str | None = None):
    """One row per (run, test) with its metrics, without raw samples: the input for analysis."""
    with connect() as conn:
        return conn.execute(
            """SELECT r.run_id, r.started_at, r.network, r.server, t.test, t.passed, t.error, t.metrics
               FROM runs r JOIN test_results t USING (run_id)
               WHERE (%(network)s::text IS NULL OR r.network = %(network)s)
                 AND (%(test)s::text IS NULL OR t.test = %(test)s)
               ORDER BY r.started_at""",
            {"network": network, "test": test},
        ).fetchall()
