"""Legacy tests use the explicitly loopback-only development mode.

Authentication integration tests override APP_AUTH_ENABLED within their fixture.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def local_test_auth_default(monkeypatch):
    monkeypatch.setenv("APP_AUTH_ENABLED", "0")
    monkeypatch.setenv("APP_BIND_ADDR", "127.0.0.1")
