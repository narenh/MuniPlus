from pathlib import Path

import pytest

from transit_editor.data import loader
from transit_editor.models.curation import Curation
from transit_editor.models.snapshot import Snapshot

FIXTURE = Path(__file__).parent.parent / "fixtures" / "transit"


@pytest.fixture
def curation() -> Curation:
    """A fresh copy per test, so a test can break it without affecting the next."""
    return loader.read(FIXTURE)[0]


@pytest.fixture
def snapshots() -> dict[str, Snapshot]:
    return loader.read(FIXTURE)[1]
