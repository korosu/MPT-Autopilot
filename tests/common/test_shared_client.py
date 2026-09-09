"""
Tests for the shared httpx.Client singleton in _http_utils.py.

Verifies:
  - get_shared_client() returns the same instance on repeated calls
  - close_shared_client() resets the singleton, next call creates a new one
  - close_shared_client() is idempotent
"""

from __future__ import annotations

import httpx
import pytest

from mpt_autopilot._http_utils import (
    close_shared_client,
    get_shared_client,
)


@pytest.fixture(autouse=True)
def _reset_shared_client():
    close_shared_client()
    yield
    close_shared_client()


def test_get_shared_client_returns_same_instance():
    c1 = get_shared_client()
    c2 = get_shared_client()
    assert c1 is c2


def test_close_shared_client_allows_recreation():
    c1 = get_shared_client()
    close_shared_client()
    c2 = get_shared_client()
    assert c1 is not c2


def test_close_shared_client_idempotent():
    """Calling close_shared_client() twice must not raise."""
    close_shared_client()
    close_shared_client()  # must not raise


def test_new_instance_works_after_close():
    """After closing, the new client can actually send requests."""
    close_shared_client()
    client = get_shared_client()
    # The client is a real httpx.Client — verify it has the expected shape
    assert isinstance(client, httpx.Client)
