"""Control-industry state API — SQLite persistence plus a generic tool dispatch.

Harnesses call POST /tools/{tool_name} with {"arguments": {...}} for every
industry tool; the REST routes stay for evals and debugging (GET /state,
GET /health). Session tools (end_call) and handoff tools never hit this server.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

INDUSTRY_DIR = Path(__file__).resolve().parent
DB_DIR = INDUSTRY_DIR / "db"
SCHEMA_PATH = DB_DIR / "schema.sql"
SEED_PATH = DB_DIR / "seed.sql"
DB_PATH = Path(os.environ.get("MIVAS_DB_PATH", str(DB_DIR / "runtime.db")))


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        seed = SEED_PATH.read_text().strip()
        if seed:
            conn.executescript(seed)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def _db() -> Any:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="control-industry state API", lifespan=lifespan)


class AppointmentCreate(BaseModel):
    date: str = Field(description="Appointment date in MM/DD/YYYY format")


class Appointment(BaseModel):
    id: int
    date: str
    created_at: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/state")
def state() -> dict[str, Any]:
    """Eval/debug dump of durable state."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, date, created_at FROM appointments ORDER BY id"
        ).fetchall()
    return {
        "appointments": [
            {"id": r["id"], "date": r["date"], "created_at": r["created_at"]} for r in rows
        ]
    }


@app.get("/appointments")
def list_appointments() -> list[Appointment]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, date, created_at FROM appointments ORDER BY id"
        ).fetchall()
    return [
        Appointment(id=r["id"], date=r["date"], created_at=r["created_at"]) for r in rows
    ]


@app.post("/appointments", status_code=201)
def create_appointment(body: AppointmentCreate) -> Appointment:
    with _db() as conn:
        cur = conn.execute("INSERT INTO appointments (date) VALUES (?)", (body.date,))
        row = conn.execute(
            "SELECT id, date, created_at FROM appointments WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=500, detail="insert failed")
    return Appointment(id=row["id"], date=row["date"], created_at=row["created_at"])


# ------------------------------------------------------------------ dispatch
# POST /tools/{tool_name} {"arguments": {...}} — the industry-agnostic contract
# every harness speaks. Results use the envelope this industry's tools.json
# outputSchema declares (here: {"success": ..., ...}).


class ToolCall(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


def _dispatch_schedule_appointment(args: dict[str, Any]) -> dict[str, Any]:
    created = create_appointment(AppointmentCreate(date=args["date"]))
    return {"success": True, "date": created.date}


DISPATCH = {
    "schedule_appointment": _dispatch_schedule_appointment,
}


@app.post("/tools/{tool_name}")
def dispatch_tool(tool_name: str, body: ToolCall) -> dict[str, Any]:
    handler = DISPATCH.get(tool_name)
    if handler is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown tool {tool_name!r} — session and handoff tools are "
            "harness-native and industry tools must be listed in DISPATCH",
        )
    try:
        return handler(dict(body.arguments or {}))
    except HTTPException as e:
        return {"success": False, "error": str(e.detail)}
    except Exception as e:  # soft-fail: a broken tool must not 500 into the call
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("TOOL_SERVER_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
