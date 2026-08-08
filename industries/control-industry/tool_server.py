"""Control-industry state API — SQLite persistence, not a 1:1 tools.json mirror.

The OpenAI (or other) harness maps agent tools onto these routes.
Harness-local tools like end_call never hit this server.
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


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("TOOL_SERVER_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
