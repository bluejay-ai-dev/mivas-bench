"""DBService in isolation — no industry tool_server imported.

Callers (industry routes) still do `with db.connect() as conn: conn.execute(...)`.
These tests only assert the file-per-call behavior behind that connection.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from db_service import CallIdError, DBService  # noqa: E402

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


def test_ensure_creates_schema_and_seed(db: DBService, tmp_path: Path) -> None:
    path = db.ensure("675")
    assert path.is_file()
    conn = sqlite3.connect(path)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM items ORDER BY id")]
    finally:
        conn.close()
    assert names == ["seeded"]
    assert path.parent == tmp_path / "calls"
    assert path.name == "675.db"


def test_second_ensure_does_not_reseed(db: DBService) -> None:
    path = db.ensure("675")
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO items (name) VALUES ('written')")
        conn.commit()
    finally:
        conn.close()
    again = db.ensure("675")
    assert again == path
    conn = sqlite3.connect(again)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM items ORDER BY id")]
    finally:
        conn.close()
    assert names == ["seeded", "written"]


def _names(db: DBService, call_id: str) -> list[str]:
    with db.connect(call_id) as conn:
        return [r[0] for r in conn.execute("SELECT name FROM items ORDER BY id")]


def test_scope_fresh_rebuilds_fixture(db: DBService) -> None:
    """A repeatable check must not inherit the previous run's mutations."""
    with db.scope("selfcheck", fresh=True):
        with db.connect() as conn:
            conn.execute("UPDATE items SET name = 'mutated' WHERE name = 'seeded'")
    assert _names(db, "selfcheck") == ["mutated"]
    with db.scope("selfcheck", fresh=True):
        assert _names(db, "selfcheck") == ["seeded"]


def test_scope_fresh_clears_wal_sidecars(db: DBService) -> None:
    path = db.ensure("selfcheck")
    with db.connect("selfcheck") as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO items (name) VALUES ('written')")
    with db.scope("selfcheck", fresh=True):
        assert _names(db, "selfcheck") == ["seeded"]
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()
    assert not list(path.parent.glob("*.tmp"))


def test_scope_without_fresh_keeps_state(db: DBService) -> None:
    """Real calls reuse their DB across requests — that must not change."""
    with db.scope("675"):
        with db.connect() as conn:
            conn.execute("INSERT INTO items (name) VALUES ('written')")
    with db.scope("675"):
        assert _names(db, "675") == ["seeded", "written"]


def test_distinct_ids_are_isolated(db: DBService) -> None:
    a = db.ensure("675")
    b = db.ensure("676")
    assert a != b
    conn = sqlite3.connect(a)
    try:
        conn.execute("INSERT INTO items (name) VALUES ('only-a')")
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect(b)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM items ORDER BY id")]
    finally:
        conn.close()
    assert names == ["seeded"]


def test_concurrent_ensure_creates_one_file(db: DBService) -> None:
    from concurrent.futures import ThreadPoolExecutor

    paths: list[Path] = []

    def once() -> None:
        paths.append(db.ensure("675"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: once(), range(16)))
    assert len({p.resolve() for p in paths}) == 1
    files = list((db.data_dir / "calls").glob("*.db"))
    assert len(files) == 1
    conn = sqlite3.connect(paths[0])
    try:
        n = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        conn.close()
    assert n == 1  # seed applied once, not 16 times


def test_empty_and_path_ids_are_rejected(db: DBService) -> None:
    for bad in ("", "   ", "../evil", "..", "foo/bar", "a" * 65):
        with pytest.raises(CallIdError):
            db.ensure(bad)


def test_connect_sees_seed_and_persists_writes(db: DBService) -> None:
    with db.scope("675"):
        with db.connect() as conn:
            names = [r["name"] for r in conn.execute("SELECT name FROM items ORDER BY id")]
            assert names == ["seeded"]
            conn.execute("INSERT INTO items (name) VALUES ('written')")
    with db.scope("675"):
        with db.connect() as conn:
            names = [r["name"] for r in conn.execute("SELECT name FROM items ORDER BY id")]
    assert names == ["seeded", "written"]


def test_connect_without_call_id_is_rejected(db: DBService) -> None:
    with pytest.raises(CallIdError):
        with db.connect():
            pass


def test_shared_mode_allows_missing_call_id(tmp_path: Path) -> None:
    schema = tmp_path / "schema.sql"
    seed = tmp_path / "seed.sql"
    schema.write_text(SCHEMA)
    seed.write_text(SEED)
    db = DBService(schema_path=schema, seed_path=seed, data_dir=tmp_path, shared=True)
    with db.connect() as conn:
        conn.execute("INSERT INTO items (name) VALUES ('debug')")
    with db.connect() as conn:
        names = [r["name"] for r in conn.execute("SELECT name FROM items ORDER BY id")]
    assert names == ["seeded", "debug"]


def test_middleware_scopes_header_and_rejects_miss(
    db: DBService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tools_http import mount as mount_tools_http

    app = FastAPI()
    app.middleware("http")(db.http_middleware)
    mount_tools_http(app, db.calls_dir)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/count")
    def count() -> dict[str, int]:
        with db.connect() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
        return {"n": n}

    @app.post("/write")
    def write() -> dict[str, str]:
        with db.connect() as conn:
            conn.execute("INSERT INTO items (name) VALUES ('x')")
        return {"ok": "true"}

    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/count").status_code == 400
    assert client.get("/count", headers={"X-Mivas-Call-Id": "675"}).json() == {"n": 1}
    assert client.post("/write", headers={"X-Mivas-Call-Id": "675"}).status_code == 200
    assert client.get("/count?call_id=675").json() == {"n": 2}
    assert client.get("/count", headers={"X-Mivas-Call-Id": "676"}).json() == {"n": 1}
    assert client.get("/count", headers={"X-Mivas-Call-Id": "../evil"}).status_code == 400
    bind = client.post("/bind", json={"provider_call_id": "vapi-1", "sim_id": "675"})
    assert bind.status_code == 200, bind.text
    assert client.get("/bind/vapi-1").json()["sim_id"] == "675"
    assert client.get("/bind/missing").status_code == 404
    a = {"appointments": [{"date": "08/18/2026"}]}
    b = {"appointments": [{"date": "09/01/2026"}]}
    assert client.post("/snapshot", json={"call_id": "675", "state": a}).status_code == 200
    assert client.post("/snapshot", json={"call_id": "676", "state": b}).status_code == 200
    assert client.get("/snapshot/675").json() == a
    assert client.get("/snapshot/676").json() == b
    assert client.get("/snapshot/677").status_code == 404
    assert client.post("/snapshot", json={"call_id": "675"}).status_code == 400
    assert client.get("/snapshot/bad.id").status_code == 400


def test_for_industry_uses_env_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    industry = tmp_path / "pack"
    db_dir = industry / "db"
    db_dir.mkdir(parents=True)
    (db_dir / "schema.sql").write_text(SCHEMA)
    (db_dir / "seed.sql").write_text(SEED)
    data = tmp_path / "data"
    monkeypatch.setenv("MIVAS_DB_PATH", str(data / "industry.db"))
    monkeypatch.delenv("MIVAS_DB_SHARED", raising=False)
    db = DBService.for_industry(industry)
    path = db.ensure("675")
    assert path == data / "calls" / "675.db"
    assert path.is_file()
    with pytest.raises(CallIdError):
        with db.connect():
            pass
