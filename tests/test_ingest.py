"""Ingestion : versionnement des états, splits, reprise, journal."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from pea import db as db_module
from pea.data import ingest as I
from pea.data.provider import NotFound, RateLimited


def _universe(con, valeurs):
    """Insère quelques valeurs actives dans l'univers."""
    rows = [
        {
            "isin": f"FR000000{i:04d}", "name": ticker, "mic": "XPAR", "mnemonic": ticker.split(".")[0],
            "quote_currency": "EUR", "isin_country": "FR", "yf_ticker": ticker,
            "resolution": "mnemonic", "consecutive_not_found": 0,
            "first_seen": dt.date(2026, 1, 1), "last_seen": dt.date(2026, 9, 9),
            "fetched_at_utc": db_module.now_utc(),
        }
        for i, ticker in enumerate(valeurs)
    ]
    db_module.insert_df(con, "universe", pd.DataFrame(rows))


def _prix(dates, closes, *, splits=None, dividends=None):
    n = len(dates)
    return pd.DataFrame(
        {
            "date": dates, "open": closes, "high": closes, "low": closes, "close": closes,
            "adj_close": closes, "volume": [1000] * n,
            "dividend": dividends or [0.0] * n, "split_ratio": splits or [0.0] * n,
        }
    )


def _etats(revenu, *, period_end=dt.date(2025, 12, 31), currency="EUR"):
    return pd.DataFrame(
        [
            {"statement": "income", "period_type": "annual", "period_end": period_end,
             "field": "TotalRevenue", "value": revenu, "currency": currency}
        ]
    )


class FakeProvider:
    def __init__(self):
        self.prix: dict[str, pd.DataFrame] = {}
        self.etats: dict[str, pd.DataFrame] = {}
        self.infos: dict[str, dict] = {}
        self.consensus_data: dict[str, dict] = {}
        self.dates: dict[str, pd.DataFrame] = {}
        self.taux: dict[str, pd.DataFrame] = {}
        self.appels: list[tuple[str, str]] = []
        self.rate_limited: set[str] = set()
        self.last_cache_path = "cache/factice"

    def prices(self, ticker, start):
        self.appels.append(("prices", ticker))
        if ticker in self.rate_limited:
            raise RateLimited("bloqué")
        if ticker not in self.prix:
            raise NotFound(ticker)
        frame = self.prix[ticker]
        return frame if start is None else frame[frame["date"] > start].reset_index(drop=True)

    def fx(self, quote_ccy, start):
        self.appels.append(("fx", quote_ccy))
        if quote_ccy not in self.taux:
            raise NotFound(quote_ccy)
        return self.taux[quote_ccy]

    def statements(self, ticker):
        self.appels.append(("statements", ticker))
        if ticker in self.rate_limited:
            raise RateLimited("bloqué")
        if ticker not in self.etats:
            raise NotFound(ticker)
        return self.etats[ticker]

    def descriptors(self, ticker):
        self.appels.append(("info", ticker))
        if ticker in self.rate_limited:
            raise RateLimited("bloqué")
        if ticker not in self.infos:
            raise NotFound(ticker)
        return self.infos[ticker]

    def consensus(self, ticker):
        self.appels.append(("consensus", ticker))
        return self.consensus_data.get(ticker, {"currency": None})

    def earnings_dates(self, ticker):
        self.appels.append(("earnings_dates", ticker))
        return self.dates.get(ticker, pd.DataFrame(columns=["event_date", "eps_estimate", "eps_reported"]))


@pytest.fixture
def provider():
    return FakeProvider()


# ------------------------------------------------------------------------- états financiers


def test_meme_etat_deux_fois_ne_cree_qu_une_ligne(con, provider):
    assert I.upsert_statements(con, "AI.PA", _etats(100.0)) == 1
    assert I.upsert_statements(con, "AI.PA", _etats(100.0)) == 0
    assert con.execute("SELECT count(*) FROM statements").fetchone()[0] == 1


def test_valeur_retraitee_ajoute_une_version(con, provider):
    I.upsert_statements(con, "AI.PA", _etats(100.0))
    assert I.upsert_statements(con, "AI.PA", _etats(105.0)) == 1
    lignes = con.execute(
        "SELECT value FROM statements ORDER BY fetched_at_utc, value"
    ).fetchall()
    assert [row[0] for row in lignes] == [100.0, 105.0]  # l'ancienne version est conservée


def test_champ_disparu_conserve_son_ancienne_valeur(con):
    I.upsert_statements(con, "AI.PA", _etats(100.0))
    I.upsert_statements(con, "AI.PA", pd.DataFrame(columns=_etats(1.0).columns))
    assert con.execute("SELECT count(*) FROM statements").fetchone()[0] == 1


# --------------------------------------------------------------------------------- cours


def test_cours_upsert_idempotent(con, provider):
    _universe(con, ["AI.PA"])
    provider.prix["AI.PA"] = _prix([dt.date(2026, 9, 7), dt.date(2026, 9, 8)], [10.0, 11.0])
    stats = I.ingest_prices(con, provider, ["AI.PA"], dt.date(2026, 9, 9))
    assert stats.ok == 1 and stats.rows == 2
    # Une relance le lendemain ne duplique rien.
    I.ingest_prices(con, provider, ["AI.PA"], dt.date(2026, 9, 10))
    assert con.execute("SELECT count(*) FROM prices").fetchone()[0] == 2


def test_correction_de_cours_dans_le_recouvrement(con, provider):
    _universe(con, ["AI.PA"])
    provider.prix["AI.PA"] = _prix([dt.date(2026, 9, 7), dt.date(2026, 9, 8)], [10.0, 11.0])
    I.ingest_prices(con, provider, ["AI.PA"], dt.date(2026, 9, 9))
    provider.prix["AI.PA"] = _prix([dt.date(2026, 9, 7), dt.date(2026, 9, 8)], [10.0, 11.5])
    I.ingest_prices(con, provider, ["AI.PA"], dt.date(2026, 9, 10))
    assert con.execute(
        "SELECT close FROM prices WHERE date = DATE '2026-09-08'"
    ).fetchone()[0] == 11.5


def test_split_declenche_un_rechargement_complet(con, provider):
    _universe(con, ["AI.PA"])
    ancien = _prix([dt.date(2026, 9, 1), dt.date(2026, 9, 2)], [100.0, 102.0])
    provider.prix["AI.PA"] = ancien
    I.ingest_prices(con, provider, ["AI.PA"], dt.date(2026, 9, 2))

    # Split 2 pour 1 : Yahoo renvoie désormais tout l'historique divisé par deux.
    apres = _prix(
        [dt.date(2026, 9, 1), dt.date(2026, 9, 2), dt.date(2026, 9, 3)],
        [50.0, 51.0, 52.0],
        splits=[0.0, 0.0, 2.0],
    )
    provider.prix["AI.PA"] = apres
    I.ingest_prices(con, provider, ["AI.PA"], dt.date(2026, 9, 3))

    lignes = con.execute("SELECT date, close FROM prices ORDER BY date").fetchall()
    assert [row[1] for row in lignes] == [50.0, 51.0, 52.0]  # plus aucun cours d'avant-split
    journal = con.execute("SELECT status FROM fetch_log WHERE endpoint = 'prices'").fetchall()
    statuts = {row[0] for row in journal}
    assert "price_reload_split" in statuts


def test_valeur_absente_est_journalisee_et_comptee(con, provider):
    _universe(con, ["INCONNU.PA"])
    stats = I.ingest_prices(con, provider, ["INCONNU.PA"], dt.date(2026, 9, 9))
    assert stats.not_found == 1
    assert con.execute(
        "SELECT status FROM fetch_log WHERE endpoint = 'prices'"
    ).fetchone()[0] == "not_found"
    assert con.execute(
        "SELECT consecutive_not_found FROM universe WHERE yf_ticker = 'INCONNU.PA'"
    ).fetchone()[0] == 1


def test_trois_absences_mettent_la_valeur_de_cote(con, provider):
    _universe(con, ["INCONNU.PA"])
    for jour in (9, 10, 11):
        I.ingest_prices(con, provider, ["INCONNU.PA"], dt.date(2026, 9, jour))
    assert con.execute(
        "SELECT resolution FROM universe WHERE yf_ticker = 'INCONNU.PA'"
    ).fetchone()[0] == "lost"
    assert I.active_tickers(con) == []


# ------------------------------------------------------------------------------- reprise


def test_relance_le_meme_jour_ne_refait_rien(con, provider):
    _universe(con, ["AI.PA"])
    provider.prix["AI.PA"] = _prix([dt.date(2026, 9, 8)], [11.0])
    I.ingest_prices(con, provider, ["AI.PA"], dt.date(2026, 9, 9))
    provider.appels.clear()
    I.ingest_prices(con, provider, ["AI.PA"], dt.date(2026, 9, 9))
    assert provider.appels == []


def test_valeurs_dues_les_plus_anciennes_d_abord(con, provider):
    _universe(con, ["A.PA", "B.PA", "C.PA"])
    hier = db_module.now_utc() - dt.timedelta(days=1)
    vieux = db_module.now_utc() - dt.timedelta(days=30)
    for ticker, quand in (("A.PA", hier), ("B.PA", vieux)):
        for endpoint in I.FUNDAMENTAL_ENDPOINTS:
            con.execute(
                "INSERT INTO fetch_log (ticker, endpoint, fetched_at_utc, status) VALUES (?, ?, ?, 'ok')",
                [ticker, endpoint, quand],
            )
    dus = I.due_tickers(con, dt.date(2026, 9, 9))
    assert "A.PA" not in dus            # rafraîchie hier
    assert dus[0] == "C.PA"             # jamais vue
    assert set(dus) == {"C.PA", "B.PA"}


# -------------------------------------------------------------------------- descripteurs


def test_descripteurs_instantanes_seulement_si_changement(con):
    info = {"sector": "Technology", "country": "France", "long_name": "X"}
    assert I.store_descriptors(con, "AI.PA", info) is True
    assert I.store_descriptors(con, "AI.PA", info) is False
    assert I.store_descriptors(con, "AI.PA", {**info, "sector": "Industrials"}) is True
    assert con.execute("SELECT count(*) FROM descriptors").fetchone()[0] == 2


def test_consensus_vide_n_est_pas_enregistre(con):
    assert I._store_consensus(con, "AI.PA", {"currency": "EUR", "eps_0y_current": None}) == 0
    assert con.execute("SELECT count(*) FROM consensus").fetchone()[0] == 0


# ----------------------------------------------------------------------------- débit limité


def test_trois_valeurs_bloquees_interrompent_l_ingestion(con, provider):
    _universe(con, ["A.PA", "B.PA", "C.PA", "D.PA"])
    for ticker in ["A.PA", "B.PA", "C.PA", "D.PA"]:
        provider.prix[ticker] = _prix([dt.date(2026, 9, 8)], [10.0])
        provider.infos[ticker] = {"sector": "Technology", "financial_currency": "EUR"}
        provider.etats[ticker] = _etats(100.0)
    provider.rate_limited = {"B.PA", "C.PA", "D.PA"}
    provider.taux["USD"] = pd.DataFrame({"date": [dt.date(2026, 9, 8)], "rate": [1.1]})

    resume = I.run_ingest(con, provider, today=dt.date(2026, 9, 9))
    assert resume.aborted is True
    # Le travail déjà fait est conservé : les cours de A.PA sont en base.
    assert con.execute("SELECT count(*) FROM prices WHERE ticker = 'A.PA'").fetchone()[0] == 1
    assert con.execute(
        "SELECT count(*) FROM fetch_log WHERE status = 'rate_limited'"
    ).fetchone()[0] >= I.MAX_CONSECUTIVE_RATE_LIMITED


def test_ingestion_complete(con, provider):
    _universe(con, ["AI.PA"])
    provider.prix["AI.PA"] = _prix([dt.date(2026, 9, 8)], [11.0])
    provider.infos["AI.PA"] = {"sector": "Basic Materials", "financial_currency": "USD",
                               "quote_currency": "EUR", "country": "France"}
    provider.etats["AI.PA"] = _etats(100.0, currency="USD")
    provider.consensus_data["AI.PA"] = {"currency": "EUR", "eps_0y_current": 6.4, "n_analysts_0y": 12}
    provider.dates["AI.PA"] = pd.DataFrame(
        [{"event_date": dt.date(2026, 2, 20), "eps_estimate": 2.1, "eps_reported": 2.2}]
    )
    provider.taux["USD"] = pd.DataFrame({"date": [dt.date(2026, 9, 8)], "rate": [1.16]})

    resume = I.run_ingest(con, provider, today=dt.date(2026, 9, 9))
    assert not resume.aborted
    assert resume.prices.ok == 1 and resume.fundamentals.ok == 1
    assert con.execute("SELECT count(*) FROM statements").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM consensus").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM earnings_dates").fetchone()[0] == 1
    # Le taux de la devise de publication est allé chercher sa série.
    assert con.execute("SELECT count(*) FROM fx_rates WHERE quote_ccy = 'USD'").fetchone()[0] == 1
    # L'euro n'a pas de taux à récupérer.
    assert con.execute("SELECT count(*) FROM fx_rates WHERE quote_ccy = 'EUR'").fetchone()[0] == 0
