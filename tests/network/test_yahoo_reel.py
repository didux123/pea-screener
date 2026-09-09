"""Vérification du fournisseur contre le vrai Yahoo. Ignoré par défaut.

À lancer quand Yahoo change son interface : `uv run pytest -m network`.
"""

from __future__ import annotations

import datetime as dt

import pytest

from pea.config import load_config
from pea.data.provider import CONSENSUS_KEYS, DESCRIPTOR_KEYS, PRICE_COLUMNS, STATEMENT_COLUMNS
from pea.data.yahoo import Pacer, YahooProvider

pytestmark = pytest.mark.network

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="module")
def provider(tmp_path_factory):
    cfg = load_config(ROOT / "config.toml")
    return YahooProvider(tmp_path_factory.mktemp("cache"), Pacer(cfg.yahoo))


def test_metadonnees_reelles(provider):
    info = provider.descriptors("AI.PA")
    assert set(info) == set(DESCRIPTOR_KEYS)
    assert info["quote_type"] == "EQUITY"
    assert info["country"] == "France"
    assert info["shares_outstanding"] and info["shares_outstanding"] > 1e8


def test_cours_reels(provider):
    frame = provider.prices("ASML.AS", dt.date.today() - dt.timedelta(days=30))
    assert list(frame.columns) == list(PRICE_COLUMNS)
    assert len(frame) > 10
    assert frame["close"].gt(0).all()
    assert frame["date"].max() >= dt.date.today() - dt.timedelta(days=7)


def test_etats_financiers_reels(provider):
    frame = provider.statements("SAP.DE")
    assert list(frame.columns) == list(STATEMENT_COLUMNS)
    revenus = frame[(frame["statement"] == "income") & (frame["field"] == "TotalRevenue")]
    assert len(revenus) >= 3
    assert revenus["value"].gt(1e9).all()


def test_consensus_reel(provider):
    data = provider.consensus("ASML.AS")
    assert set(data) == set(CONSENSUS_KEYS)
    assert data["n_analysts_0y"] and data["n_analysts_0y"] > 5


def test_resolution_reelle_par_isin(provider):
    """L'ISIN vient des listes de bourse : Yahoo doit savoir le retrouver."""
    assert provider.resolve("FR0000120073", "MNEMONIQUE_FAUX", "XPAR").ticker == "AI.PA"
