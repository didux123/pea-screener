"""Fixtures communes. Aucun test (hors marqueur `network`) ne doit toucher le réseau."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from pea import db as db_module

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Fait échouer tout appel réseau involontaire pendant les tests hors marqueur `network`."""
    if request.node.get_closest_marker("network"):
        return
    import socket

    def _blocked(*args, **kwargs):  # pragma: no cover - déclenché seulement en cas de régression
        raise RuntimeError("Appel réseau interdit dans les tests (marqueur `network` absent)")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


@pytest.fixture
def con(tmp_path):
    """Base DuckDB neuve, schéma appliqué."""
    connection = db_module.connect(tmp_path / "test.duckdb")
    yield connection
    connection.close()


@pytest.fixture
def utc_now() -> dt.datetime:
    return dt.datetime(2026, 9, 9, 20, 0, 0)
