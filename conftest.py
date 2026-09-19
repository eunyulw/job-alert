"""Pytest bootstrap that makes the shared test helpers importable.

The tests live outside any installed package, so pytest's default import mode does not put the
repository root on ``sys.path``. Adapter tests import the shared assertions from
``tests.helpers``, so add the repository root and the ``tests`` directory explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from job_alert.config import AppConfig, load_app_config

ROOT = Path(__file__).resolve().parent

for entry in (ROOT, ROOT / "tests"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))


@pytest.fixture()
def app_config() -> AppConfig:
    """The repository's canonical company, source, and filter configuration."""
    return load_app_config(ROOT / "config")
