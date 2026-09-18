"""Pytest bootstrap that makes the shared test helpers importable.

The tests live outside any installed package, so pytest's default import mode does not put the
repository root on ``sys.path``. Adapter tests import the shared assertions from
``tests.helpers``, so add the repository root and the ``tests`` directory explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

for entry in (ROOT, ROOT / "tests"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))
