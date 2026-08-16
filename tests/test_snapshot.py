"""B3: capture_final freezes GET /state locally and PUTs JSON + sqlite to S3."""

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
from snapshot import capture_final, snapshot_key  # noqa: E402
from tools_http import mount as mount_tools_http  # noqa: E402

SCHEMA = """
CREATE TABLE items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);
"""
SEED = "INSERT INTO items (name) VALUES ('seeded');"


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DBService:
    schema = tmp_path / "schema.sql"
    seed = tmp_path / "seed.sql"
    schema.write_text(SCHEMA)
    seed.write_text(SEED)
    monkeypatch.setenv("MIVAS_DB_PATH", str(tmp_path / "industry.db"))
    monkeypatch.setenv("MIVAS_SLUG", "openai-realtime-2-1-control-industry")
    monkeypatch.delenv("MIVAS_SNAPSHOT_BUCKET", raising=False)
    return DBService(schema_path=schema, seed_path=seed, data_dir=tmp_path)


def _app(db: DBService) -> FastAPI:
    app = FastAPI()
    app.middleware("http")(db.http_middleware)
    mount_tools_http(app, db.calls_dir)

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


def test_capture_final_writes_local_and_s3(
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
    monkeypatch.setenv("MIVAS_SNAPSHOT_BUCKET", "mivas-call-dbs")
    puts: list[tuple[str, bytes, str]] = []

    def fake_put(key: str, body: bytes, content_type: str) -> None:
        puts.append((key, body, content_type))

    monkeypatch.setattr("snapshot._put_s3", fake_put)

    client = TestClient(app)
    client.post(
        "/write", headers={"X-Mivas-Call-Id": "721435"}, params={"name": "booked"}
    )
    dumped = capture_final("721435")
    assert dumped == {"items": ["seeded", "booked"]}
    frozen = json.loads((db.calls_dir / "721435.final.json").read_text())
    assert frozen == dumped
    keys = [p[0] for p in puts]
    assert snapshot_key("721435", ".final.json") in keys
    assert snapshot_key("721435", ".db") in keys
    json_body = next(b for k, b, _ in puts if k.endswith(".final.json"))
    assert json.loads(json_body) == dumped
    assert json.loads(json_body) != {"items": ["seeded"]}

    server.should_exit = True
    thread.join(timeout=5)


def test_capture_final_skips_s3_when_bucket_unset(
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
    assert server.started
    monkeypatch.setenv("TOOL_SERVER_URL", f"http://127.0.0.1:{port}")
    monkeypatch.delenv("MIVAS_SNAPSHOT_BUCKET", raising=False)
    client = TestClient(app)
    client.post("/write", headers={"X-Mivas-Call-Id": "800"}, params={"name": "local"})
    dumped = capture_final("800")
    assert dumped == {"items": ["seeded", "local"]}
    assert json.loads((db.calls_dir / "800.final.json").read_text()) == dumped
    server.should_exit = True
    thread.join(timeout=5)


def test_capture_final_two_ids_do_not_share_s3_keys(
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
    assert server.started
    monkeypatch.setenv("TOOL_SERVER_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("MIVAS_SNAPSHOT_BUCKET", "mivas-call-dbs")
    puts: list[str] = []
    monkeypatch.setattr(
        "snapshot._put_s3", lambda key, body, content_type: puts.append(key)
    )
    client = TestClient(app)
    client.post("/write", headers={"X-Mivas-Call-Id": "675"}, params={"name": "a"})
    client.post("/write", headers={"X-Mivas-Call-Id": "676"}, params={"name": "b"})
    capture_final("675")
    capture_final("676")
    assert snapshot_key("675", ".final.json") in puts
    assert snapshot_key("676", ".final.json") in puts
    assert snapshot_key("675", ".final.json") != snapshot_key("676", ".final.json")
    server.should_exit = True
    thread.join(timeout=5)
