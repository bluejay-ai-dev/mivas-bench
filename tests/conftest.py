"""The public repo ships six sample tasks per industry; the full 72-task suite is private.
Tests marked full_suite read held-out tasks and skip unless that suite is checked out."""

from pathlib import Path

import pytest

INDUSTRIES = Path(__file__).resolve().parents[1] / "industries"
FULL_SUITE = all(
    len(list((INDUSTRIES / industry / "tasks").glob("*/task.json"))) == 72
    for industry in ("healthcare", "legal", "customer-support")
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "full_suite: needs the private 72-task suite")


def pytest_runtest_setup(item: pytest.Item) -> None:
    if not FULL_SUITE and item.get_closest_marker("full_suite"):
        pytest.skip("needs the private 72-task suite")
