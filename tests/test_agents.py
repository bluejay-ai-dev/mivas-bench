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
    pair_websocket_url,
    parse_agents,
    render_agents_yaml,
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


def test_render_agents_yaml_two_pairs() -> None:
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
    assert f"name: mivas-{slug('nvidia/nemotron', 'control-industry')}" in yaml_text
    assert 'mivas.harness_family: "openai"' in yaml_text
    assert 'mivas.harness_runtime: "realtime-2.1"' in yaml_text
    assert 'mivas.harness_family: "nvidia"' in yaml_text
    assert 'mivas.harness_runtime: "nemotron"' in yaml_text
    assert "__SLUG__" not in yaml_text
    assert "__HARNESS_RUNTIME__" not in yaml_text


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
    # "kind: Ingress" is a prefix of IngressClass / IngressClassParams; match the
    # resource kind line exactly.
    assert yaml_text.count("\nkind: Ingress\n") == 1
    assert yaml_text.count("kind: Deployment") == 3
    assert "host: openai-realtime-2-1-control-industry.benchmarks.example.com" in yaml_text
    assert "livekit-cascaded-control-industry.benchmarks.example.com" not in yaml_text
    assert "pipecat-openai-realtime-2-1-control-industry.benchmarks.example.com" not in yaml_text
    assert 'name: MIVAS_MODE\n              value: "chirp"' in yaml_text
    assert 'name: MIVAS_MODE\n              value: "agent"' in yaml_text
    from run import pair_host, pair_mivas_mode, pair_needs_ingress

    assert pair_needs_ingress("openai/realtime-2.1")
    assert not pair_needs_ingress("livekit/cascaded")
    assert pair_mivas_mode("pipecat/cascaded") == "agent"
    assert pair_host("livekit/cascaded", "control-industry") is None


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
        _ecr_registry_host("148660429236.dkr.ecr.us-west-1.amazonaws.com/mivas-bench")
        == "148660429236.dkr.ecr.us-west-1.amazonaws.com"
    )
    assert _ecr_registry_host("ghcr.io/bluejay/mivas-bench") is None
