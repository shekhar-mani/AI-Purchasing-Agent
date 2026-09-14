"""
Runs before any test module is imported. Points the app at an isolated
SQLite file and forces rule-based reasoning mode so the automated test suite
is deterministic and reproducible (no dependency on network/API availability
or LLM non-determinism). See EVALUATION.md for how the LLM-driven mode is
evaluated separately.
"""
import os
import sys
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite:///./test_purchasing_agent.db"
os.environ.pop("ANTHROPIC_API_KEY", None)

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import pytest


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    from app import seed

    seed.reset_and_seed()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db_session():
    from app.database import SessionLocal
    from app import seed

    seed.reset_and_seed()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
