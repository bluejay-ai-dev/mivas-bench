"""Tests for AGENTS parsing and multi-doc Kubernetes rendering."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from run import (  # noqa: E402
    image_ref,
    pair_host,
    pair_public_url,
    pair_resources,
    pair_websocket_url,
    parse_agents,
    render_agents_yaml,
    replica_count,
    slug,
)


def test_parse_agents_basic() -> None:
    assert parse_agents(
        "openai/realtime-2.1:healthcare,nvidia/nemotron:control-industry"
    ) == [
        ("openai/realtime-2.1", "healthcare"),
        ("nvidia/nemotron", "control-industry"),
    ]


def test_parse_agents_strips_whitespace() -> None:
    assert parse_agents("  openai/realtime-2.1:legal , nvidia/nemotron:travel ") == [
        ("openai/realtime-2.1", "legal"),
        ("nvidia/nemotron", "travel"),
    ]


def test_parse_agents_rejects_bad_entry() -> None:
    with pytest.raises(ValueError, match="family/runtime:industry"):
        parse_agents("openai-only")
    with pytest.raises(ValueError, match="family/runtime"):
        parse_agents("openai:healthcare")  # missing runtime slash


def test_render_agents_yaml_two_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIVAS_REPLICAS", raising=False)
    yaml_text = render_agents_yaml(
        [
            ("openai/realtime-2.1", "healthcare"),
            ("nvidia/nemotron", "control-industry"),
        ],
        "LoadBalancer",
    )
    assert yaml_text.count("kind: Deployment") == 2
    assert yaml_text.count("kind: Service") == 2
    assert "name: tools" in yaml_text
    assert "---" in yaml_text
    assert f"name: mivas-{slug('openai/realtime-2.1', 'healthcare')}" in yaml_text
    assert f"name: mivas-{slug('openai/realtime-2.1', 'healthcare')}-tools" not in yaml_text
    assert f"name: mivas-{slug('nvidia/nemotron', 'control-industry')}" in yaml_text
    assert f"name: mivas-{slug('nvidia/nemotron', 'control-industry')}-tools" not in yaml_text
    assert 'mivas.harness_family: "openai"' in yaml_text
    assert 'mivas.harness_runtime: "realtime-2.1"' in yaml_text
    assert 'mivas.harness_family: "nvidia"' in yaml_text
    assert 'mivas.harness_runtime: "nemotron"' in yaml_text
    assert "__SLUG__" not in yaml_text
    assert "__HARNESS_RUNTIME__" not in yaml_text
    assert "Thank you for calling Straus Dermatology." in yaml_text
    assert "Welcome to Bluejay's Repair Services!" in yaml_text
    assert "__TWILIO_WELCOME_GREETING__" not in yaml_text
    assert yaml_text.count("\n  replicas: 1\n") == 2
    assert "__REPLICAS__" not in yaml_text
    assert "http://127.0.0.1:8000" in yaml_text
    assert "-tools:8000" not in yaml_text


def test_stable_ingress_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIVAS_BASE_DOMAIN", "chirp.example.com")
    monkeypatch.setenv(
        "MIVAS_ACM_CERTIFICATE_ARN",
        "arn:aws:acm:us-east-1:123456789012:certificate/abcd",
    )
    harness, industry = "openai/realtime-2.1", "healthcare"
    s = slug(harness, industry)
    assert pair_host(harness, industry) == f"{s}.chirp.example.com"
    assert pair_websocket_url(harness, industry) == f"wss://{s}.chirp.example.com"
    assert pair_public_url(harness, industry) == f"https://{s}.chirp.example.com"

    yaml_text = render_agents_yaml([(harness, industry)], "ClusterIP")
    assert "kind: Ingress" in yaml_text
    assert "kind: IngressClass" in yaml_text
    assert "kind: IngressClassParams" in yaml_text
    assert "ingressClassName: mivas-alb" in yaml_text
    assert f"host: {s}.chirp.example.com" in yaml_text
    assert f"https://{s}.chirp.example.com" in yaml_text
    assert "mivas-chirp" in yaml_text
    assert "arn:aws:acm:us-east-1:123456789012:certificate/abcd" in yaml_text
    assert "alb.ingress.kubernetes.io/healthcheck-path: /health" in yaml_text
    assert 'alb.ingress.kubernetes.io/healthcheck-port: "8000"' in yaml_text
    assert "https://otlp.getbluejay.ai/v1/traces" in yaml_text
    assert "https://api.getbluejay.ai/v1" in yaml_text
    assert "maxUnavailable: 0" in yaml_text
    assert "maxSurge: 1" in yaml_text
    # maxUnavailable only guards rollouts. Karpenter consolidation evicted a pod
    # mid-run ("Evicted pod: Underutilized"), killing six live calls and the
    # trace POST that runs in the harness's finally block.
    assert 'karpenter.sh/do-not-disrupt: "true"' in yaml_text
    assert (
        "alb.ingress.kubernetes.io/target-group-attributes: "
        "load_balancing.algorithm.type=least_outstanding_requests"
    ) in yaml_text
    assert "stickiness.enabled" not in yaml_text
    assert f"name: mivas-{s}-tools" not in yaml_text
    assert "path: /state" not in yaml_text
    assert "path: /snapshot" not in yaml_text
    assert "path: /\n" in yaml_text
    assert "http://127.0.0.1:8000" in yaml_text


def test_worker_families_skip_ingress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIVAS_BASE_DOMAIN", "benchmarks.example.com")
    monkeypatch.setenv(
        "MIVAS_ACM_CERTIFICATE_ARN",
        "arn:aws:acm:us-east-1:123456789012:certificate/abcd",
    )
    yaml_text = render_agents_yaml(
        [
            ("openai/realtime-2.1", "control-industry"),
            ("livekit/cascaded", "control-industry"),
            ("pipecat/openai-realtime-2.1", "control-industry"),
        ],
        "ClusterIP",
    )
    # CHIRP pair: one Ingress `/`. Workers: tools-only Ingress `/tools`.
    assert yaml_text.count("\nkind: Ingress\n") == 3
    assert yaml_text.count("kind: Deployment") == 3
    assert "host: openai-realtime-2-1-control-industry.benchmarks.example.com" in yaml_text
    assert "host: livekit-cascaded-control-industry.benchmarks.example.com" in yaml_text
    assert "host: pipecat-openai-realtime-2-1-control-industry.benchmarks.example.com" in yaml_text
    assert yaml_text.count("path: /tools") == 2
    assert 'name: MIVAS_MODE\n              value: "chirp"' in yaml_text
    assert 'name: MIVAS_MODE\n              value: "agent"' in yaml_text
    from run import pair_host, pair_mivas_mode, pair_needs_ingress

    assert pair_needs_ingress("openai/realtime-2.1")
    assert not pair_needs_ingress("livekit/cascaded")
    assert pair_mivas_mode("pipecat/cascaded") == "agent"
    assert pair_mivas_mode("openai/realtime-2.1") == "chirp"
    assert pair_host("livekit/cascaded", "control-industry") is None
    from run import pair_dns_host, pair_public_url

    assert (
        pair_dns_host("livekit/cascaded", "control-industry")
        == "livekit-cascaded-control-industry.benchmarks.example.com"
    )
    assert pair_public_url("pipecat/cascaded", "control-industry") == (
        "https://pipecat-cascaded-control-industry.benchmarks.example.com"
    )


def test_twilio_ingress_is_conversationrelay() -> None:
    from run import ingress_adapter, pair_mivas_mode

    harness = "twilio/conversationrelay-gpt4.1"
    assert pair_mivas_mode(harness) == "conversationrelay"
    assert ingress_adapter(harness).name == "conversationrelay.py"
    yaml_text = render_agents_yaml([(harness, "control-industry")], "LoadBalancer")
    assert 'name: MIVAS_MODE\n              value: "conversationrelay"' in yaml_text
    assert 'name: MIVAS_MODE\n              value: "chirp"' not in yaml_text


def test_image_ref_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIVAS_IMAGE_PREFIX", raising=False)
    assert image_ref("openai/realtime-2.1", "healthcare") == (
        f"mivas-bench:{slug('openai/realtime-2.1', 'healthcare')}"
    )
    monkeypatch.setenv(
        "MIVAS_IMAGE_PREFIX",
        "123.dkr.ecr.us-east-1.amazonaws.com/mivas-bench",
    )
    assert image_ref("openai/realtime-2.1", "healthcare") == (
        f"123.dkr.ecr.us-east-1.amazonaws.com/mivas-bench:"
        f"{slug('openai/realtime-2.1', 'healthcare')}"
    )


def test_ecr_registry_host() -> None:
    from run import _ecr_registry_host

    assert (
        _ecr_registry_host("123456789012.dkr.ecr.us-east-1.amazonaws.com/mivas-bench")
        == "123456789012.dkr.ecr.us-east-1.amazonaws.com"
    )
    assert _ecr_registry_host("ghcr.io/bluejay/mivas-bench") is None


def test_replica_count_defaults_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIVAS_REPLICAS", raising=False)
    assert replica_count() == 1
    monkeypatch.setenv("MIVAS_REPLICAS", "")
    assert replica_count() == 1
    monkeypatch.setenv("MIVAS_REPLICAS", " 2 ")
    assert replica_count() == 2


def test_replica_count_rejects_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIVAS_REPLICAS", "0")
    with pytest.raises(ValueError, match="MIVAS_REPLICAS"):
        replica_count()
    monkeypatch.setenv("MIVAS_REPLICAS", "-1")
    with pytest.raises(ValueError, match="MIVAS_REPLICAS"):
        replica_count()
    monkeypatch.setenv("MIVAS_REPLICAS", "two")
    with pytest.raises(ValueError, match="MIVAS_REPLICAS"):
        replica_count()


def test_render_respects_mivas_replicas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIVAS_REPLICAS", "3")
    yaml_text = render_agents_yaml(
        [("openai/realtime-2.1", "control-industry")],
        "LoadBalancer",
    )
    s = slug("openai/realtime-2.1", "control-industry")
    assert f"name: mivas-{s}-tools" not in yaml_text
    assert "\n  replicas: 3\n" in yaml_text
    assert "\n  replicas: 1\n" not in yaml_text
    assert "http://127.0.0.1:8000" in yaml_text
    assert "__REPLICAS__" not in yaml_text
    assert "MIVAS_SNAPSHOT_BUCKET" in yaml_text
    # no serviceAccountName => no creds => every snapshot PUT silently fails
    assert "serviceAccountName: mivas-bench" in yaml_text


def test_render_snapshot_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIVAS_SNAPSHOT_BUCKET", "mivas-call-dbs")
    monkeypatch.setenv("MIVAS_SNAPSHOT_PREFIX", "call-freeze")
    yaml_text = render_agents_yaml(
        [("openai/realtime-2.1", "control-industry")],
        "LoadBalancer",
    )
    assert "mivas-call-dbs" in yaml_text
    assert "call-freeze" in yaml_text
    assert "__SNAPSHOT_BUCKET__" not in yaml_text


def test_cascaded_nemotron_gets_heavier_pod() -> None:
    assert pair_resources("nvidia/nemotron") == ("1000m", "1Gi", "3Gi")
    assert pair_resources("nvidia/nemotron-voicechat") == ("250m", "384Mi", "1536Mi")
    assert pair_resources("openai/realtime-2.1") == ("250m", "384Mi", "1536Mi")
    yaml_text = render_agents_yaml(
        [
            ("openai/realtime-2.1", "healthcare"),
            ("nvidia/nemotron", "healthcare"),
        ],
        "LoadBalancer",
    )
    assert yaml_text.count("cpu: 1000m") == 1
    assert yaml_text.count("cpu: 250m") == 1
    assert "__CPU_REQUEST__" not in yaml_text
    assert "__MEMORY_LIMIT__" not in yaml_text
