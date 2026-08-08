"""Healthcare state API — SQLite persistence, not a 1:1 tools.json mirror.

The OpenAI (or other) harness maps agent tools onto these routes.
Harness-local tools like end_call never hit this server.
"""

from __future__ import annotations

import json
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


app = FastAPI(title="healthcare state API", lifespan=lifespan)


class AppointmentCreate(BaseModel):
    patient_id: str | None = None
    location_id: str
    provider_id: str
    appointment_type_code: str
    start: str
    end: str
    description: str = ""


class AppointmentUpdate(BaseModel):
    start: str | None = None
    end: str | None = None
    location_id: str | None = None
    provider_id: str | None = None
    status: str | None = Field(default=None, description="booked | cancelled | completed")
    description: str | None = None


class WaitlistCreate(BaseModel):
    patient_id: str | None = None
    appointment_type_code: str
    location_ids: list[str]
    earliest: str | None = None
    latest: str | None = None


def _appointment(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "patient_id": row["patient_id"],
        "location_id": row["location_id"],
        "provider_id": row["provider_id"],
        "appointment_type_code": row["appointment_type_code"],
        "start": row["start"],
        "end": row["end"],
        "description": row["description"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/state")
def state() -> dict[str, Any]:
    """Eval/debug dump of durable state."""
    with _db() as conn:
        patients = [dict(r) for r in conn.execute("SELECT * FROM patients ORDER BY id")]
        locations = [dict(r) for r in conn.execute("SELECT * FROM locations ORDER BY id")]
        providers = [dict(r) for r in conn.execute("SELECT * FROM providers ORDER BY id")]
        appointments = [
            _appointment(r)
            for r in conn.execute("SELECT * FROM appointments ORDER BY id")
        ]
        waitlist = []
        for r in conn.execute("SELECT * FROM waitlist ORDER BY id"):
            waitlist.append(
                {
                    "id": r["id"],
                    "patient_id": r["patient_id"],
                    "appointment_type_code": r["appointment_type_code"],
                    "location_ids": json.loads(r["location_ids"]),
                    "earliest": r["earliest"],
                    "latest": r["latest"],
                    "created_at": r["created_at"],
                }
            )
    return {
        "patients": patients,
        "locations": locations,
        "providers": providers,
        "appointments": appointments,
        "waitlist": waitlist,
    }


@app.get("/locations")
def list_locations() -> list[dict[str, Any]]:
    with _db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM locations ORDER BY id")]


@app.get("/providers")
def list_providers() -> list[dict[str, Any]]:
    with _db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM providers ORDER BY id")]


@app.get("/patients")
def list_patients() -> list[dict[str, Any]]:
    with _db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM patients ORDER BY id")]


@app.get("/patients/{patient_id}")
def get_patient(patient_id: str) -> dict[str, Any]:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="patient not found")
    return dict(row)


@app.get("/appointments")
def list_appointments() -> list[dict[str, Any]]:
    with _db() as conn:
        return [
            _appointment(r)
            for r in conn.execute("SELECT * FROM appointments ORDER BY id")
        ]


@app.post("/appointments", status_code=201)
def create_appointment(body: AppointmentCreate) -> dict[str, Any]:
    with _db() as conn:
        if body.location_id and conn.execute(
            "SELECT 1 FROM locations WHERE id = ?", (body.location_id,)
        ).fetchone() is None:
            raise HTTPException(status_code=400, detail="unknown location_id")
        if body.provider_id and conn.execute(
            "SELECT 1 FROM providers WHERE id = ?", (body.provider_id,)
        ).fetchone() is None:
            raise HTTPException(status_code=400, detail="unknown provider_id")
        cur = conn.execute(
            """
            INSERT INTO appointments (
              patient_id, location_id, provider_id, appointment_type_code,
              start, end, description
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                body.patient_id,
                body.location_id,
                body.provider_id,
                body.appointment_type_code,
                body.start,
                body.end,
                body.description,
            ),
        )
        row = conn.execute(
            "SELECT * FROM appointments WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=500, detail="insert failed")
    return _appointment(row)


@app.patch("/appointments/{appointment_id}")
def update_appointment(appointment_id: int, body: AppointmentUpdate) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [appointment_id]
    with _db() as conn:
        cur = conn.execute(
            f"UPDATE appointments SET {cols} WHERE id = ?", vals
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="appointment not found")
        row = conn.execute(
            "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
        ).fetchone()
    assert row is not None
    return _appointment(row)


@app.post("/waitlist", status_code=201)
def join_waitlist(body: WaitlistCreate) -> dict[str, Any]:
    with _db() as conn:
        cur = conn.execute(
            """
            INSERT INTO waitlist (
              patient_id, appointment_type_code, location_ids, earliest, latest
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                body.patient_id,
                body.appointment_type_code,
                json.dumps(body.location_ids),
                body.earliest,
                body.latest,
            ),
        )
        row = conn.execute(
            "SELECT * FROM waitlist WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=500, detail="insert failed")
    return {
        "id": row["id"],
        "patient_id": row["patient_id"],
        "appointment_type_code": row["appointment_type_code"],
        "location_ids": json.loads(row["location_ids"]),
        "earliest": row["earliest"],
        "latest": row["latest"],
        "created_at": row["created_at"],
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("TOOL_SERVER_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
