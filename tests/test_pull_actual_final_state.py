"""Pull S3 hangup dumps into slug/run/dh/result and compare against expected."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pull = _load("pull_actual_final_state", ROOT / "scripts" / "pull_actual_final_state.py")
efs = _load("expected_final_state", ROOT / "scripts" / "expected_final_state.py")


def test_pair_slug_and_s3_key() -> None:
    assert pull.pair_slug("openai/realtime-2.1", "customer-support") == (
        "openai-realtime-2-1-customer-support"
    )
    assert pull.s3_final_key("openai-realtime-2-1-customer-support", "738848") == (
        "mivas/openai-realtime-2-1-customer-support/738848.final.json"
    )


def test_actual_path_is_slug_run_dh_result(tmp_path: Path) -> None:
    path = pull.actual_path(tmp_path, "slug-a", "100", "200", "300")
    assert path == tmp_path / "slug-a" / "100" / "200" / "300" / "final.json"


def test_pull_run_writes_k3_siblings(tmp_path: Path) -> None:
    states = {
        "mivas/slug-a/11.final.json": {"orders": [{"id": 1}]},
        "mivas/slug-a/12.final.json": {"orders": [{"id": 1}]},
        "mivas/slug-a/21.final.json": {"orders": [{"id": 2}]},
    }

    def list_results(_run_id: str):
        return (
            {"id": "100", "simulation_id": "9"},
            [
                {"id": 11, "digital_human_id": 200, "status": "COMPLETED"},
                {"id": 12, "digital_human_id": 200, "status": "COMPLETED"},
                {"id": 21, "digital_human_id": 201, "status": "COMPLETED"},
                {"id": 99, "digital_human_id": 202, "status": "COMPLETED"},
            ],
        )

    manifest = pull.pull_run(
        "100",
        "slug-a",
        tmp_path,
        get_json=lambda key: states.get(key),
        list_results=list_results,
    )
    assert manifest["fetched"] == 3
    assert manifest["missing"] == 1
    run_dir = tmp_path / "slug-a" / "100"
    assert (run_dir / "200" / "11" / "final.json").is_file()
    assert (run_dir / "200" / "12" / "final.json").is_file()
    assert (run_dir / "201" / "21" / "final.json").is_file()
    assert not (run_dir / "202" / "99" / "final.json").exists()
    index = json.loads((run_dir / "index.json").read_text())
    assert index["run_id"] == "100"
    missing = [r for r in index["results"] if not r["fetched"]]
    assert missing[0]["result_id"] == 99


def test_compare_actuals_reports_per_case(tmp_path: Path) -> None:
    expected_dir = tmp_path / "expected" / "customer-support"
    expected_dir.mkdir(parents=True)
    (expected_dir / "T1-E1.final.json").write_text(json.dumps({"orders": []}) + "\n")
    (expected_dir / "T1-H1.final.json").write_text(
        json.dumps({"orders": [{"id": 1, "date": "2026-08-18"}]}) + "\n"
    )
    (expected_dir / "T2-E1.final.json").write_text(json.dumps({"orders": []}) + "\n")
    (expected_dir / "index.json").write_text(json.dumps({
        "industry": "customer-support",
        "cases": [
            {"case_key": "T1-E1", "digital_human_id": 200, "path": "T1-E1.final.json"},
            {"case_key": "T1-H1", "digital_human_id": 201, "path": "T1-H1.final.json"},
            {"case_key": "T2-E1", "digital_human_id": 202, "path": "T2-E1.final.json"},
        ],
    }) + "\n")

    run_dir = tmp_path / "actuals" / "slug-a" / "100"
    for dh_id, result_id in (("200", "11"), ("200", "12"), ("201", "21")):
        pull.actual_path(tmp_path / "actuals", "slug-a", "100", dh_id, result_id).parent.mkdir(
            parents=True
        )
    (run_dir / "200" / "11" / "final.json").write_text(json.dumps({"orders": []}) + "\n")
    (run_dir / "200" / "12" / "final.json").write_text(json.dumps({"orders": []}) + "\n")
    (run_dir / "201" / "21" / "final.json").write_text(
        json.dumps({"orders": [{"id": 1, "date": "wrong", "created_at": "now"}]}) + "\n"
    )

    report = efs.compare_actuals(expected_dir, run_dir)
    by_key = {c["case_key"]: c for c in report["cases"]}
    assert by_key["T1-E1"]["matched"] == 2
    assert by_key["T1-E1"]["mismatched"] == 0
    assert by_key["T1-H1"]["matched"] == 0
    assert by_key["T1-H1"]["mismatched"] == 1
    assert report["matched"] == 2
    assert report["mismatched"] == 1
    assert report["cases_all_match"] == 1
    assert report["cases_any_mismatch"] == 1
    assert report["cases_with_no_actuals"] == ["T2-E1"]
    assert (run_dir / "report.json").is_file()


def test_compare_actuals_cli_infers_industry(tmp_path: Path) -> None:
    expected_dir = tmp_path / "expected-final-state" / "customer-support"
    expected_dir.mkdir(parents=True)
    (expected_dir / "T1-E1.final.json").write_text(json.dumps({"orders": []}) + "\n")
    (expected_dir / "index.json").write_text(json.dumps({
        "industry": "customer-support",
        "cases": [
            {"case_key": "T1-E1", "digital_human_id": 200, "path": "T1-E1.final.json"},
        ],
    }) + "\n")
    slug = "openai-realtime-2-1-customer-support"
    dest = pull.actual_path(tmp_path / "actuals", slug, "100", "200", "11")
    dest.parent.mkdir(parents=True)
    dest.write_text(json.dumps({"orders": []}) + "\n")
    rc = efs.main([
        "--compare-actuals", str(tmp_path / "actuals" / slug / "100"),
        "--out", str(tmp_path / "expected-final-state"),
    ])
    assert rc == 0


def test_industry_from_slug() -> None:
    assert efs.industry_from_slug("openai-realtime-2-1-customer-support") == "customer-support"
    assert efs.industry_from_slug("livekit-cascaded-healthcare") == "healthcare"
    assert efs.industry_from_slug("control-industry") == "control-industry"
    assert efs.industry_from_slug("unknown") is None
