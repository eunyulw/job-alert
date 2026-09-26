"""The ``service_harness`` fixture shared by the collection-cycle integration tests.

The harness itself lives in ``tests/integration/harness.py`` so a test can name its types; this
module only builds one over a temporary database with the provider endpoint mocked offline.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import respx

from tests.integration.harness import ServiceHarness


@pytest.fixture()
async def service_harness(tmp_path: Path) -> AsyncIterator[ServiceHarness]:
    """A wired service over a temporary database, with the endpoint mocked offline."""
    with respx.mock(assert_all_called=False) as router:
        harness = ServiceHarness(tmp_path, router)
        try:
            yield harness
        finally:
            await harness.fetcher.close()
            harness.repo.close()
