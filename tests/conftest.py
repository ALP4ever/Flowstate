from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from database import initialize_database
from engine import FlowStateEngine


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    initialize_database(tmp_path)
    return tmp_path


@pytest.fixture()
def engine(project_root: Path) -> FlowStateEngine:
    return FlowStateEngine(project_root)
