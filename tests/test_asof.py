"""Le calcul à une date passée n'utilise que ce qui était connu à cette date."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from pea import db as db_module
from pea.screen import asof as A

AS_OF = dt.date(2026, 3, 15)
CUTOFF = dt.datetime(2026, 3, 16)


def _fetch_log(con, quand: dt.datetime, ticker="AI.PA", endpoint="statements"):
    con.execute(
        "INSERT INTO fetch_log (ticker, endpoint, fetched_at_utc, status) VALUES (?, ?, ?, 'ok')",
        [ticker, endpoint, quand],
    )


def _statement(
    con, ticker, period_end, field, value, fetched, *, statement="income", period_type="annual"
):
    db_module.insert_df(
        con,
        "statements",
        pd.DataFrame([{
            "ticker": ticker, "statement": statement, "period_type": period_type,
            "period_end": period_end, "field": field, "value": value,
            "currency": "EUR", "fetched_at_utc": fetched,
        }]),
    )


def _price(con, ticker, date, close, fetched=dt.datetime(2026, 3, 10)):
    db_module.insert_df(
        con,
        "prices",
        pd.DataFrame([{
            "ticker": ticker, "date": date, "close": close, "open": close, "high": close,
            "low": close, "adj_close": close, "volume": 1000, "dividend": 0.0,
            "split_ratio": 0.0, "fetched_at_utc": fetched,
        }]),
    )


def _universe_row(con, isin="FR0000120073", ticker="AI.PA", first_seen=dt.date(2026, 1, 5), delisted=None):
    db_module.insert_df(
        con,
        "universe",
        pd.DataFrame([{
            "isin": isin, "name": "AIR LIQUIDE", "mic": "XPAR", "mnemonic": "AI",
            "quote_currency": "EUR", "isin_country": "FR", "yf_ticker": ticker,
            "resolution": "mnemonic", "consecutive_not_found": 0,
            "first_seen": first_seen, "last_seen": dt.date(2026, 9, 9),
            "delisted_at": delisted, "fetched_at_utc": dt.datetime(2026, 1, 5),
        }]),
    )


# ------------------------------------------------------------------------ dates de disponibilité


def test_delai_reglementaire_quand_la_publication_est_inconnue():
    # Exercice clos le 31 décembre : au plus tard connu 120 jours après, soit le 30 avril.
    assert A.available_from(dt.date(2025, 12, 31), "annual", None, None) == dt.date(2026, 4, 30)


def test_date_de_publication_reelle_prime_sur_le_delai():
    publiee = dt.date(2026, 2, 20)
    assert A.available_from(dt.date(2025, 12, 31), "annual", None, publiee) == publiee


def test_premiere_recuperation_prime_quand_elle_est_anterieure():
    """Si le système a vu la donnée le 3 mars, elle existait le 3 mars."""
    assert A.available_from(
        dt.date(2025, 12, 31), "annual", dt.date(2026, 3, 3), dt.date(2026, 4, 10)
    ) == dt.date(2026, 3, 3)


def test_date_de_resultats_trop_proche_de_la_cloture_est_ignoree():
    # Une date à J+5 est un communiqué de chiffre d'affaires, pas la publication des comptes.
    assert A.published_at_for(dt.date(2025, 12, 31), "annual", [dt.date(2026, 1, 5)]) is None
    assert A.published_at_for(dt.date(2025, 12, 31), "annual", [dt.date(2026, 2, 20)]) == dt.date(2026, 2, 20)


def test_date_de_resultats_trop_lointaine_est_ignoree():
    trop_tard = dt.date(2025, 12, 31) + dt.timedelta(days=200)
    assert A.published_at_for(dt.date(2025, 12, 31), "annual", [trop_tard]) is None


def test_cutoff_est_la_fin_de_journee():
    assert A.cutoff_utc(AS_OF) == CUTOFF


# ------------------------------------------------------------------------------ versions


def test_version_anterieure_choisie_puis_version_suivante(con):
    _statement(con, "AI.PA", dt.date(2025, 12, 31), "TotalRevenue", 100.0, dt.datetime(2026, 2, 1))
    _statement(con, "AI.PA", dt.date(2025, 12, 31), "TotalRevenue", 105.0, dt.datetime(2026, 6, 1))

    avant = A.statements_at(con, AS_OF, CUTOFF)
    assert list(avant["value"]) == [100.0]        # le retraitement de juin n'existait pas
    assert not avant["from_later_fetch"].any()

    apres = A.statements_at(con, dt.date(2026, 7, 1), A.cutoff_utc(dt.date(2026, 7, 1)))
    assert list(apres["value"]) == [105.0]


def test_calcul_retrospectif_prend_la_version_la_plus_ancienne_et_le_signale(con):
    _statement(con, "AI.PA", dt.date(2024, 12, 31), "TotalRevenue", 90.0, dt.datetime(2026, 6, 1))
    frame = A.statements_at(con, dt.date(2025, 6, 30), A.cutoff_utc(dt.date(2025, 6, 30)))
    assert list(frame["value"]) == [90.0]
    assert frame["from_later_fetch"].all()  # la donnée a été récupérée après la date de calcul


def test_etat_non_encore_publie_est_absent(con):
    # Exercice clos fin 2025, récupéré en juin 2026 : au 15 mars il n'était pas publié.
    _statement(con, "AI.PA", dt.date(2025, 12, 31), "TotalRevenue", 100.0, dt.datetime(2026, 6, 1))
    assert A.statements_at(con, AS_OF, CUTOFF).empty


# --------------------------------------------------------------------------- chargements


def test_cours_posterieurs_absents(con):
    _price(con, "AI.PA", dt.date(2026, 3, 13), 170.0)
    _price(con, "AI.PA", dt.date(2026, 3, 16), 999.0)
    frame = A.prices_until(con, AS_OF)
    assert list(frame["close"]) == [170.0]


def test_consensus_perime_ecarte(con):
    db_module.insert_df(con, "consensus", pd.DataFrame([{
        "ticker": "AI.PA", "fetched_at_utc": dt.datetime(2026, 1, 5), "eps_0y_current": 6.0,
    }]))
    assert A.consensus_at(con, AS_OF, CUTOFF).empty  # plus de trente jours


def test_consensus_recent_retenu(con):
    db_module.insert_df(con, "consensus", pd.DataFrame([{
        "ticker": "AI.PA", "fetched_at_utc": dt.datetime(2026, 3, 10), "eps_0y_current": 6.0,
    }]))
    assert len(A.consensus_at(con, AS_OF, CUTOFF)) == 1


def test_consensus_posterieur_ecarte(con):
    db_module.insert_df(con, "consensus", pd.DataFrame([{
        "ticker": "AI.PA", "fetched_at_utc": dt.datetime(2026, 3, 20), "eps_0y_current": 9.9,
    }]))
    assert A.consensus_at(con, AS_OF, CUTOFF).empty


def test_descripteur_anterieur_au_premier_instantane_est_signale(con):
    db_module.insert_df(con, "descriptors", pd.DataFrame([{
        "ticker": "AI.PA", "fetched_at_utc": dt.datetime(2026, 6, 1), "sector": "Basic Materials",
    }]))
    frame = A.descriptors_at(con, CUTOFF)
    assert frame.iloc[0]["sector"] == "Basic Materials"
    assert bool(frame.iloc[0]["from_later_fetch"]) is True


def test_taux_de_change_trop_ancien_ecarte(con):
    db_module.insert_df(con, "fx_rates", pd.DataFrame([{
        "quote_ccy": "USD", "date": dt.date(2026, 2, 1), "rate": 1.1,
        "fetched_at_utc": dt.datetime(2026, 2, 1),
    }]))
    assert A.fx_at(con, AS_OF, CUTOFF).empty


# ----------------------------------------------------------------------------- régimes


def test_regime_live_et_reconstruit(con):
    _fetch_log(con, dt.datetime(2026, 3, 1))
    assert A.detect_mode(con, dt.date(2026, 3, 15)) == "live"
    assert A.detect_mode(con, dt.date(2026, 2, 15)) == "reconstructed"


def test_univers_live_respecte_les_dates_d_entree_et_de_sortie(con):
    _universe_row(con, "FR0000120073", "AI.PA", first_seen=dt.date(2026, 1, 5))
    _universe_row(con, "FR0000121014", "MC.PA", first_seen=dt.date(2026, 6, 1))
    _universe_row(con, "FR0010313833", "AKE.PA", first_seen=dt.date(2026, 1, 5),
                  delisted=dt.date(2026, 2, 1))
    presentes = set(A.universe_at(con, AS_OF, "live")["isin"])
    assert presentes == {"FR0000120073"}  # l'une n'existait pas encore, l'autre était radiée


# ------------------------------------------------------------- la preuve : aucune fuite


@pytest.fixture
def base_complete(con):
    """Une base cohérente au 15 mars 2026, pour douze valeurs."""
    for i in range(12):
        isin, ticker = f"FR000000{i:04d}", f"T{i}.PA"
        _universe_row(con, isin, ticker, first_seen=dt.date(2026, 1, 5))
        for jour in range(1, 15):
            _price(con, ticker, dt.date(2026, 3, jour), 100.0 + i + jour * 0.1)
        _statement(con, ticker, dt.date(2024, 12, 31), "TotalRevenue", 1000.0 + i,
                   dt.datetime(2026, 1, 10))
        _statement(con, ticker, dt.date(2024, 12, 31), "OperatingIncome", 100.0 + i,
                   dt.datetime(2026, 1, 10))
        db_module.insert_df(con, "descriptors", pd.DataFrame([{
            "ticker": ticker, "fetched_at_utc": dt.datetime(2026, 1, 10),
            "sector": "Technology", "country": "France", "financial_currency": "EUR",
        }]))
        db_module.insert_df(con, "consensus", pd.DataFrame([{
            "ticker": ticker, "fetched_at_utc": dt.datetime(2026, 3, 10),
            "eps_0y_current": 6.0 + i, "eps_0y_90d": 5.5 + i, "n_analysts_0y": 8,
            "up_30d_0y": 3, "down_30d_0y": 1,
        }]))
        _fetch_log(con, dt.datetime(2026, 1, 10), ticker=ticker)
    db_module.insert_df(con, "fx_rates", pd.DataFrame([{
        "quote_ccy": "USD", "date": dt.date(2026, 3, 13), "rate": 1.16,
        "fetched_at_utc": dt.datetime(2026, 3, 13),
    }]))
    return con


def _empoisonne(con):
    """Ajoute des données postérieures au 15 mars : elles ne doivent rien changer."""
    for i in range(12):
        ticker = f"T{i}.PA"
        # Exercice suivant, publié plus tard.
        _statement(con, ticker, dt.date(2025, 12, 31), "TotalRevenue", 9_999_999.0,
                   dt.datetime(2026, 6, 1))
        # Retraitement de l'exercice déjà connu.
        _statement(con, ticker, dt.date(2024, 12, 31), "TotalRevenue", 8_888_888.0,
                   dt.datetime(2026, 6, 1))
        # Cours postérieurs aberrants.
        for jour in (16, 17, 18):
            _price(con, ticker, dt.date(2026, 3, jour), 1e9, fetched=dt.datetime(2026, 3, jour))
        # Changement de secteur constaté plus tard.
        db_module.insert_df(con, "descriptors", pd.DataFrame([{
            "ticker": ticker, "fetched_at_utc": dt.datetime(2026, 7, 1),
            "sector": "Utilities", "country": "France", "financial_currency": "EUR",
        }]))
        # Consensus révisé après coup.
        db_module.insert_df(con, "consensus", pd.DataFrame([{
            "ticker": ticker, "fetched_at_utc": dt.datetime(2026, 4, 1),
            "eps_0y_current": 99.0, "eps_0y_90d": 1.0, "n_analysts_0y": 30,
        }]))
        # Date de résultats future.
        db_module.insert_df(con, "earnings_dates", pd.DataFrame([{
            "ticker": ticker, "event_date": dt.date(2026, 4, 25), "eps_estimate": 1.0,
            "eps_reported": 1.0, "fetched_at_utc": dt.datetime(2026, 5, 1),
        }]))
    # Une valeur entrée dans l'univers après la date de calcul.
    _universe_row(con, "FR0000999999", "NOUVEAU.PA", first_seen=dt.date(2026, 8, 1))


def _photo(pit) -> dict:
    return {
        "univers": sorted(pit.universe["isin"]),
        "etats": pit.statements[["ticker", "period_end", "field", "value"]]
        .sort_values(["ticker", "period_end", "field"]).to_csv(index=False),
        "cours": pit.prices[["ticker", "date", "close"]].sort_values(["ticker", "date"]).to_csv(index=False),
        "secteurs": pit.descriptors[["ticker", "sector"]].sort_values("ticker").to_csv(index=False),
        "consensus": pit.consensus[["ticker", "eps_0y_current"]].sort_values("ticker").to_csv(index=False),
        "change": pit.fx[["quote_ccy", "rate"]].to_csv(index=False),
    }


def test_aucune_donnee_posterieure_n_entre_dans_le_calcul(base_complete):
    """La preuve : ajouter des données postérieures ne change pas d'un iota le résultat."""
    avant = _photo(A.load_pit(base_complete, AS_OF))
    _empoisonne(base_complete)
    apres_pit = A.load_pit(base_complete, AS_OF)
    apres = _photo(apres_pit)

    assert avant == apres
    assert apres_pit.audit.max_fetched_at_used < CUTOFF
    assert apres_pit.audit.max_info_date_used <= AS_OF
    assert "FR0000999999" not in apres["univers"]
    assert "9999999" not in apres["etats"] and "8888888" not in apres["etats"]
    assert "1000000000" not in apres["cours"]
    assert "Utilities" not in apres["secteurs"]


def test_audit_renseigne(base_complete):
    pit = A.load_pit(base_complete, AS_OF)
    assert pit.mode == "live"
    assert pit.survivorship_complete is True
    assert pit.audit.max_fetched_at_used is not None
    assert pit.audit.max_info_date_used is not None
