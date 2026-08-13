"""B3: capture_final freezes GET /state onto GET /snapshot/{id}."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from db_service import DBService  # noqa: E402
from snapshot import capture_final  # noqa: E402

SCHEMA = """
CREATE TABLE items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);
"""
SEED = "INSERT INTO items (name) VALUES ('seeded');"


@pytest.fixture
def db(tmp_path: Path) -> DBService:
    schema = tmp_path / "schema.sql"
    seed = tmp_path / "seed.sql"
    schema.write_text(SCHEMA)
    seed.write_text(SEED)
    return DBService(schema_path=schema, seed_path=seed, data_dir=tmp_path)


def _app(db: DBService) -> FastAPI:
    app = FastAPI()
    app.middleware("http")(db.http_middleware)
    db.mount_cluster_routes(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/state")
    def state() -> dict[str, list[str]]:
        with db.connect() as conn:
            names = [r["name"] for r in conn.execute("SELECT name FROM items ORDER BY id")]
        return {"items": names}

    @app.post("/write")
    def write(name: str) -> dict[str, str]:
        with db.connect() as conn:
            conn.execute("INSERT INTO items (name) VALUES (?)", (name,))
        return {"ok": "true"}

    return app


def test_snapshots_do_not_substitute_a_second_call(db: DBService) -> None:
    client = TestClient(_app(db))
    client.post("/write", headers={"X-Mivas-Call-Id": "675"}, params={"name": "alpha"})
    client.post("/write", headers={"X-Mivas-Call-Id": "676"}, params={"name": "beta"})
    a = client.get("/state", params={"call_id": "675"}).json()
    b = client.get("/state", params={"call_id": "676"}).json()
    assert a == {"items": ["seeded", "alpha"]}
    assert b == {"items": ["seeded", "beta"]}
    assert client.post("/snapshot", json={"call_id": "675", "state": a}).status_code == 200
    assert client.post("/snapshot", json={"call_id": "676", "state": b}).status_code == 200
    assert client.get("/snapshot/675").json() == a
    assert client.get("/snapshot/676").json() == b
    assert client.get("/snapshot/675").json() != client.get("/snapshot/676").json()


def test_capture_final_writes_snapshot(
    db: DBService, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    app = _app(db)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(80):
        if server.started:
            break
        thread.join(0.05)
    assert server.started, "uvicorn failed to start"
    monkeypatch.setenv("TOOL_SERVER_URL", f"http://127.0.0.1:{port}")

    client = TestClient(app)
    client.post(
        "/write", headers={"X-Mivas-Call-Id": "721435"}, params={"name": "booked"}
    )
    dumped = capture_final("721435")
    assert dumped == {"items": ["seeded", "booked"]}
    frozen = json.loads((db.calls_dir / "721435.final.json").read_text())
    assert frozen == dumped
    assert client.get("/snapshot/721435").json() == dumped

    server.should_exit = True
    thread.join(timeout=5)
