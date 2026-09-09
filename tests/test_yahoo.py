"""Le fournisseur Yahoo, rejoué sur des réponses figées (aucun accès réseau)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from pea.config import YahooLimits
from pea.data import yahoo as Y
from pea.data.provider import CONSENSUS_KEYS, DESCRIPTOR_KEYS, NotFound
from pea.data.yahoo import Pacer, YahooProvider

FIXTURES = Path(__file__).resolve().parent / "fixtures"  # le fournisseur y ajoute « yahoo »
CAPTURE_DATE = dt.date(2026, 9, 9)
PRICE_START = dt.date(2025, 1, 2)


class HorlogeSimulee:
    """Horloge et temporisation factices : les tests ne dorment jamais."""

    def __init__(self):
        self.instant = 0.0
        self.pauses: list[float] = []

    def sleep(self, seconds):
        self.pauses.append(seconds)
        self.instant += seconds

    def clock(self):
        self.instant += 0.001
        return self.instant


@pytest.fixture
def horloge():
    return HorlogeSimulee()


class PacerSansReseau(Pacer):
    """Toute requête réelle fait échouer le test : les fixtures doivent suffire."""

    def tick(self):
        raise AssertionError("appel réseau : la réponse figée manque dans tests/fixtures/yahoo")


@pytest.fixture
def provider(horloge):
    limits = YahooLimits(requests_per_pause=100, pause_seconds=30, min_interval_seconds=0.5)
    return YahooProvider(
        FIXTURES,
        PacerSansReseau(limits, sleep=horloge.sleep, clock=horloge.clock),
        today=CAPTURE_DATE,
    )


# ------------------------------------------------------------------------------ limiteur


def test_pause_apres_n_requetes(horloge):
    pacer = Pacer(
        YahooLimits(requests_per_pause=3, pause_seconds=30, min_interval_seconds=0),
        sleep=horloge.sleep,
        clock=horloge.clock,
    )
    for _ in range(7):
        pacer.tick()
    assert horloge.pauses == [30, 30]  # après la 3e et la 6e requête


def test_intervalle_minimal_respecte(horloge):
    pacer = Pacer(
        YahooLimits(requests_per_pause=100, pause_seconds=30, min_interval_seconds=0.5),
        sleep=horloge.sleep,
        clock=horloge.clock,
    )
    pacer.tick()
    pacer.tick()
    assert horloge.pauses and 0 < horloge.pauses[0] <= 0.5


def test_temporisation_croissante_puis_abandon(horloge):
    pacer = Pacer(
        YahooLimits(requests_per_pause=100, pause_seconds=30, min_interval_seconds=0),
        sleep=horloge.sleep,
        clock=horloge.clock,
    )
    for attempt in range(3):
        pacer.backoff(attempt)
    assert horloge.pauses == [120, 240, 480]
    with pytest.raises(Y.RateLimited):
        pacer.backoff(3)


def test_lecture_du_cache_ne_consomme_aucune_requete(provider, horloge):
    provider.descriptors("AI.PA")
    provider.prices("AI.PA", PRICE_START)
    provider.statements("AI.PA")
    assert horloge.pauses == []  # aucun appel réseau, donc aucune attente


# ------------------------------------------------------------------------- normalisation


def test_descripteurs(provider):
    info = provider.descriptors("AI.PA")
    assert set(info) == set(DESCRIPTOR_KEYS)
    assert info["long_name"] == "L'Air Liquide S.A."
    assert info["sector"] == "Basic Materials"
    assert info["country"] == "France"
    assert info["quote_type"] == "EQUITY"
    assert info["financial_currency"] == "EUR"
    assert info["shares_outstanding"] > 6e8


def test_descripteurs_devise_de_publication_differente(provider):
    info = provider.descriptors("SBMO.AS")
    assert info["quote_currency"] == "EUR"
    assert info["financial_currency"] == "USD"  # conversion nécessaire pour les multiples


def test_symbole_inconnu(provider):
    with pytest.raises(NotFound):
        provider.descriptors("SYMBOLE_ABSENT.PA")


def test_cours_bruts(provider):
    prices = provider.prices("AI.PA", PRICE_START)
    assert list(prices.columns) == list(Y.PRICE_COLUMNS)
    assert isinstance(prices["date"].iloc[0], dt.date)
    assert prices["date"].is_monotonic_increasing
    assert prices["close"].notna().all()
    assert prices["dividend"].notna().all() and prices["split_ratio"].notna().all()
    assert (prices["dividend"] > 0).any()  # Air Liquide détache un dividende chaque année


def test_taux_de_change(provider):
    fx = provider.fx("USD", PRICE_START)
    assert list(fx.columns) == list(Y.FX_COLUMNS)
    assert 0.8 < fx["rate"].iloc[-1] < 1.6  # 1 EUR vaut de l'ordre d'un dollar


def test_etats_financiers(provider):
    frame = provider.statements("AI.PA")
    assert list(frame.columns) == list(Y.STATEMENT_COLUMNS)
    assert set(frame["statement"]) == {"income", "balance", "cashflow"}
    assert set(frame["currency"]) == {"EUR"}
    revenus = frame[
        (frame["statement"] == "income")
        & (frame["period_type"] == "annual")
        & (frame["field"] == "TotalRevenue")
    ]
    assert len(revenus) >= 4  # quatre exercices au moins
    assert (revenus["value"] > 1e10).all()
    assert isinstance(revenus["period_end"].iloc[0], dt.date)


def test_publication_semestrielle_sans_trimestriels(provider):
    """Virbac publie deux fois par an : Yahoo ne fournit aucun compte de résultat trimestriel."""
    frame = provider.statements("VIRP.PA")
    trimestriel = frame[(frame["period_type"] == "quarterly") & (frame["statement"] == "income")]
    assert trimestriel.empty
    annuel = frame[(frame["period_type"] == "annual") & (frame["statement"] == "income")]
    assert not annuel.empty


def test_etats_en_dollars(provider):
    frame = provider.statements("SBMO.AS")
    assert set(frame["currency"]) == {"USD"}


def test_consensus(provider):
    data = provider.consensus("AI.PA")
    assert set(data) == set(CONSENSUS_KEYS)
    assert data["n_analysts_0y"] >= 5
    assert data["eps_0y_current"] and data["eps_0y_90d"]
    assert data["up_30d_0y"] is not None and data["down_30d_0y"] is not None


def test_consensus_zero_de_yahoo_traite_comme_absent():
    trend = pd.DataFrame(
        {"current": [3.4, 6.4], "90daysAgo": [0.0, 6.1], "currency": ["EUR", "EUR"]},
        index=["+1q", "0y"],
    )
    data = Y.normalize_consensus(trend, None, None)
    assert data["eps_0y_90d"] == 6.1
    assert data["eps_1y_90d"] is None  # ligne absente du tableau, pas imputée


def test_dates_de_publication_passees_seulement(provider):
    frame = provider.earnings_dates("ASML.AS")
    assert list(frame.columns) == list(Y.EARNINGS_COLUMNS)
    assert not frame.empty
    assert frame["event_date"].max() <= CAPTURE_DATE
    assert frame["event_date"].is_unique


def test_normalisation_tableaux_vides():
    assert Y.normalize_history(pd.DataFrame()).empty
    assert Y.normalize_statement(pd.DataFrame(), "income", "annual", "EUR").empty
    assert Y.normalize_earnings_dates(None, CAPTURE_DATE).empty
    assert Y.normalize_info(None)["sector"] is None
    assert Y.normalize_consensus(None, None, None)["eps_0y_current"] is None


def test_valeur_absente_ne_devient_jamais_zero():
    raw = pd.DataFrame(
        {pd.Timestamp("2025-12-31"): [100.0, float("nan")]},
        index=["TotalRevenue", "OperatingIncome"],
    )
    frame = Y.normalize_statement(raw, "income", "annual", "EUR")
    assert list(frame["field"]) == ["TotalRevenue"]  # la ligne absente n'est pas créée


# -------------------------------------------------------------------------- résolution


def test_resolution_par_mnemonique(provider):
    result = provider.resolve("FR0000120073", "AI", "XPAR")
    assert result.ticker == "AI.PA"
    assert result.method == "mnemonic"
    assert result.descriptors["sector"] == "Basic Materials"


def test_resolution_par_isin_quand_le_mnemonique_echoue(provider):
    """Le mnémonique ne donne rien chez Yahoo : la résolution passe par l'ISIN."""
    result = provider.resolve("FR0000120073", "MNEMO_FAUX", "XPAR")
    assert result.ticker == "AI.PA"
    assert result.method == "lookup"


def test_resolution_impossible(provider):
    result = provider.resolve("FR0000000000", "SYMBOLE_ABSENT", "XPAR")
    assert result.ticker is None and result.method == "unresolved"


def test_defaut_de_fixture_fait_echouer_le_test(provider):
    """Garde-fou : sans réponse figée, le test ne doit pas silencieusement passer au réseau."""
    with pytest.raises(AssertionError, match="appel réseau"):
        provider.descriptors("VALEUR.SANS.FIXTURE")


def test_place_hors_perimetre_non_resolue(provider):
    assert provider.resolve("IT0003128367", "ENEL", "XMIL").ticker is None


@pytest.mark.parametrize(
    "info,suffixe,attendu",
    [
        ({"quoteType": "EQUITY", "exchange": "PAR", "longName": "x"}, "PA", True),
        ({"quoteType": "EQUITY", "exchange": "AMS", "longName": "x"}, "PA", False),
        ({"quoteType": "ETF", "exchange": "PAR", "longName": "x"}, "PA", False),
        ({"trailingPegRatio": None}, "PA", False),
        ({}, "PA", False),
    ],
)
def test_validation_des_metadonnees(info, suffixe, attendu):
    assert YahooProvider._info_matches(info, suffixe) is attendu
