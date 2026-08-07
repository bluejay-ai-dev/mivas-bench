"""FastAPI tool server for control-industry. Routes match tools.json (non-handoff tools)."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from jsonschema import ValidationError, validate

INDUSTRY_DIR = Path(__file__).resolve().parent
DB_DIR = INDUSTRY_DIR / "db"
SCHEMA_PATH = DB_DIR / "schema.sql"
SEED_PATH = DB_DIR / "seed.sql"
TOOLS_PATH = INDUSTRY_DIR / "tools.json"
BLUEPRINT_PATH = INDUSTRY_DIR / "agent_blueprint.json"

# Writable DB path: container uses /data; local default is db/runtime.db
DB_PATH = Path(os.environ.get("MIVAS_DB_PATH", str(DB_DIR / "runtime.db")))


def _handoff_tool_names() -> set[str]:
    blueprint = json.loads(BLUEPRINT_PATH.read_text())
    names: set[str] = set()
    for agent in blueprint.get("agents", []):
        for tool in agent.get("tools", []):
            if tool.get("handoff"):
                names.add(tool["name"])
    return names


def _tool_catalog() -> dict[str, dict[str, Any]]:
    data = json.loads(TOOLS_PATH.read_text())
    handoffs = _handoff_tool_names()
    return {t["name"]: t for t in data["tools"] if t["name"] not in handoffs}


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


def _validate(instance: Any, schema: dict[str, Any], label: str) -> None:
    try:
        validate(instance=instance, schema=schema)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=f"{label} validation failed: {e.message}") from e


def schedule_appointment(args: dict[str, Any]) -> dict[str, Any]:
    date = args["date"]
    with _db() as conn:
        conn.execute("INSERT INTO appointments (date) VALUES (?)", (date,))
    return {"success": True, "date": date}


def end_call(args: dict[str, Any]) -> dict[str, Any]:
    reason = args["reason"]
    with _db() as conn:
        conn.execute(
            "INSERT INTO call_events (event, reason) VALUES (?, ?)",
            ("end_call", reason),
        )
    return {"success": True}


HANDLERS: dict[str, Any] = {
    "schedule_appointment": schedule_appointment,
    "end_call": end_call,
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    missing = set(_tool_catalog()) - set(HANDLERS)
    if missing:
        raise RuntimeError(f"tool_server missing handlers for: {sorted(missing)}")
    yield


app = FastAPI(title="control-industry tool server", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/state")
def state() -> dict[str, Any]:
    with _db() as conn:
        appointments = conn.execute(
            "SELECT id, date, created_at FROM appointments ORDER BY id"
        ).fetchall()
        call_events = conn.execute(
            "SELECT id, event, reason, created_at FROM call_events ORDER BY id"
        ).fetchall()
    return {
        "appointments": [
            {"id": r["id"], "date": r["date"], "created_at": r["created_at"]}
            for r in appointments
        ],
        "call_events": [
            {
                "id": r["id"],
                "event": r["event"],
                "reason": r["reason"],
                "created_at": r["created_at"],
            }
            for r in call_events
        ],
    }


@app.post("/tools/{tool_name}")
def invoke_tool(
    tool_name: str,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    catalog = _tool_catalog()
    if tool_name not in catalog:
        raise HTTPException(status_code=404, detail=f"unknown tool: {tool_name}")
    handler = HANDLERS.get(tool_name)
    if handler is None:
        raise HTTPException(status_code=501, detail=f"no handler for: {tool_name}")

    spec = catalog[tool_name]
    _validate(body, spec["inputSchema"], "input")
    result = handler(body)
    _validate(result, spec["outputSchema"], "output")
    return result


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("TOOL_SERVER_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
