"""Métriques : formules, cas limites, et le principe « rien n'est imputé »."""

from __future__ import annotations

import datetime as dt
import math

import pandas as pd
import pytest

from pea.screen import metrics as M

AS_OF = dt.date(2026, 3, 15)


def _prices(dates, closes, *, volumes=None, dividends=None):
    n = len(dates)
    return pd.DataFrame(
        {
            "date": dates, "close": closes, "volume": volumes or [1000] * n,
            "dividend": dividends or [0.0] * n, "split_ratio": [0.0] * n,
        }
    )


def _serie_quotidienne(fin: dt.date, jours: int, prix=100.0, *, croissance=0.0):
    dates, closes = [], []
    jour = fin - dt.timedelta(days=jours)
    while jour <= fin:
        if jour.weekday() < 5:
            dates.append(jour)
            closes.append(prix * (1 + croissance) ** len(dates))
        jour += dt.timedelta(days=1)
    return _prices(dates, closes)


def _statements(par_exercice: dict[dt.date, dict[str, dict[str, float]]], currency="EUR"):
    lignes = []
    for period_end, etats in par_exercice.items():
        for statement, champs in etats.items():
            for field, value in champs.items():
                lignes.append({
                    "period_end": period_end, "statement": statement,
                    "field": field, "value": value, "currency": currency,
                })
    return pd.DataFrame(lignes, columns=["period_end", "statement", "field", "value", "currency"])


def _inputs(**kwargs):
    defauts = dict(
        ticker="T.PA", as_of=AS_OF,
        statements=pd.DataFrame(columns=["period_end", "statement", "field", "value", "currency"]),
        prices=pd.DataFrame(columns=["date", "close", "volume", "dividend", "split_ratio"]),
        splits=pd.DataFrame(columns=["date", "split_ratio"]),
        descriptor={}, consensus=None, fx_rate=1.0, quote_fx_rate=1.0,
    )
    return M.StockInputs(**{**defauts, **kwargs})


# ------------------------------------------------------------------------------ croissance


def test_cagr_trois_ans():
    assert M._cagr(133.1, 100.0, 1096) == pytest.approx(0.10, abs=2e-3)


@pytest.mark.parametrize("fin,debut", [(-10.0, 100.0), (100.0, -10.0), (100.0, 0.0)])
def test_cagr_impossible_sur_base_non_positive(fin, debut):
    assert M._cagr(fin, debut, 1096) is None


def test_croissance_absente_si_ecart_d_exercices_irregulier():
    etats = _statements({
        dt.date(2025, 12, 31): {"income": {"TotalRevenue": 133.1, "OperatingIncome": 20.0}},
        dt.date(2023, 12, 31): {"income": {"TotalRevenue": 100.0, "OperatingIncome": 10.0}},
    })
    m = M.compute_stock_metrics(_inputs(statements=etats))
    assert m.values["rev_cagr_3y"] is None       # deux ans d'écart, pas trois
    assert "ecart_d_exercices_irregulier" in m.flags


def test_croissance_marge_et_rentabilite():
    etats = _statements({
        dt.date(2025, 12, 31): {
            "income": {"TotalRevenue": 133.1, "OperatingIncome": 26.62, "NetIncome": 20.0},
            "balance": {"TotalAssets": 200.0, "CurrentLiabilities": 50.0},
        },
        dt.date(2022, 12, 31): {
            "income": {"TotalRevenue": 100.0, "OperatingIncome": 15.0},
        },
    })
    m = M.compute_stock_metrics(_inputs(statements=etats))
    assert m.values["rev_cagr_3y"] == pytest.approx(0.10, abs=2e-3)
    assert m.values["op_margin"] == pytest.approx(0.20)
    assert m.values["op_margin_trend_3y"] == pytest.approx(0.05)   # 20 % contre 15 %
    assert m.values["roce"] == pytest.approx(26.62 / 150.0)


def test_rentabilite_absente_si_capitaux_employes_negatifs():
    etats = _statements({dt.date(2025, 12, 31): {
        "income": {"TotalRevenue": 100.0, "OperatingIncome": 10.0},
        "balance": {"TotalAssets": 50.0, "CurrentLiabilities": 80.0},
    }})
    assert M.compute_stock_metrics(_inputs(statements=etats)).values["roce"] is None


def test_conversion_en_tresorerie_absente_si_perte():
    etats = _statements({dt.date(2025, 12, 31): {
        "income": {"TotalRevenue": 100.0, "NetIncome": -5.0},
        "cashflow": {"FreeCashFlow": 10.0},
    }})
    assert M.compute_stock_metrics(_inputs(statements=etats)).values["cash_conversion"] is None


# -------------------------------------------------------------------------------- cours


def test_indice_de_rendement_total_integre_le_dividende():
    dates = [dt.date(2026, 3, 10), dt.date(2026, 3, 11), dt.date(2026, 3, 12)]
    sans = M.total_return_index(_prices(dates, [100.0, 100.0, 100.0]))
    avec = M.total_return_index(_prices(dates, [100.0, 100.0, 100.0], dividends=[0.0, 0.0, 2.0]))
    assert sans.iloc[-1] == pytest.approx(1.0)
    assert avec.iloc[-1] == pytest.approx(1.02)


def test_momentum_douze_mois_hors_dernier_mois():
    dates, closes = [], []
    jour = dt.date(2025, 1, 1)
    while jour <= AS_OF:
        dates.append(jour)
        closes.append(100.0 if jour < dt.date(2025, 6, 1) else 120.0)
        jour += dt.timedelta(days=1)
    m = M.compute_stock_metrics(_inputs(prices=_prices(dates, closes)))
    assert m.values["mom_12_1"] == pytest.approx(0.20)


def test_momentum_absent_si_historique_trop_court():
    m = M.compute_stock_metrics(_inputs(prices=_serie_quotidienne(AS_OF, 60)))
    assert m.values["mom_12_1"] is None
    assert "prices.history_12m" in m.missing_fields


def test_moyenne_mobile_absente_si_trop_peu_de_seances():
    assert M.sma200_distance(_serie_quotidienne(AS_OF, 100), AS_OF) is None


def test_ecart_a_la_moyenne_mobile():
    prices = _serie_quotidienne(AS_OF, 400, prix=100.0)
    prices.loc[prices.index[-1], "close"] = 200.0
    ecart = M.sma200_distance(prices, AS_OF)
    assert ecart is not None and ecart > 0.9


def test_cours_trop_ancien_n_est_pas_utilise():
    vieux = _prices([dt.date(2026, 1, 10)], [100.0])
    m = M.compute_stock_metrics(_inputs(prices=vieux))
    assert m.price is None and "prices.current" in m.missing_fields


def test_liquidite_complete_les_seances_manquantes_par_des_zeros():
    # Vingt séances à un million : la médiane sur soixante-trois séances attendues est nulle.
    dates = [AS_OF - dt.timedelta(days=i) for i in range(20)]
    prices = _prices(dates, [100.0] * 20, volumes=[10_000] * 20)
    assert M.median_traded_value(prices, AS_OF) == 0.0


def test_liquidite_d_une_valeur_qui_traite_tous_les_jours():
    prices = _serie_quotidienne(AS_OF, 120, prix=100.0)
    prices["volume"] = 10_000
    assert M.median_traded_value(prices, AS_OF) == pytest.approx(1_000_000, rel=0.01)


# --------------------------------------------------------------------------------- splits


def test_facteur_de_split():
    splits = pd.DataFrame({"date": [dt.date(2025, 6, 1), dt.date(2026, 1, 15)], "split_ratio": [2.0, 3.0]})
    assert M.split_factor(splits, dt.date(2024, 12, 31), dt.date(2026, 3, 15)) == 6.0
    assert M.split_factor(splits, dt.date(2025, 6, 1), dt.date(2026, 3, 15)) == 3.0
    assert M.split_factor(splits, dt.date(2026, 2, 1), dt.date(2026, 3, 15)) == 1.0


def test_capitalisation_tient_compte_du_split_posterieur_a_l_exercice():
    """Yahoo n'ajuste pas les nombres d'actions du bilan : il faut le faire nous-mêmes."""
    etats = _statements({dt.date(2025, 12, 31): {
        "income": {"TotalRevenue": 100.0, "OperatingIncome": 10.0, "NetIncomeCommonStockholders": 8.0},
        "balance": {"OrdinarySharesNumber": 1_000_000.0, "TotalDebt": 0.0, "CashAndCashEquivalents": 0.0},
        "cashflow": {"FreeCashFlow": 9.0},
    }})
    splits = pd.DataFrame({"date": [dt.date(2026, 2, 1)], "split_ratio": [2.0]})
    prices = _prices([AS_OF], [50.0])   # le cours a été divisé par deux
    m = M.compute_stock_metrics(_inputs(statements=etats, prices=prices, splits=splits))
    assert m.market_cap_eur == pytest.approx(100_000_000.0)  # inchangée, comme il se doit


def test_dilution_annule_l_effet_d_un_split():
    etats = _statements({
        dt.date(2025, 12, 31): {"balance": {"OrdinarySharesNumber": 2_000_000.0}},
        dt.date(2022, 12, 31): {"balance": {"OrdinarySharesNumber": 1_000_000.0}},
    })
    splits = pd.DataFrame({"date": [dt.date(2024, 6, 1)], "split_ratio": [2.0]})
    m = M.compute_stock_metrics(_inputs(statements=etats, splits=splits))
    assert m.values["dilution_3y"] == pytest.approx(0.0)   # un split n'est pas une dilution


# ---------------------------------------------------------------------------- valorisation


def _valeur_complete(**remplacements):
    champs = {
        "income": {"TotalRevenue": 1000.0, "OperatingIncome": 100.0, "EBITDA": 150.0,
                   "InterestExpense": 10.0, "NetIncome": 60.0,
                   "NetIncomeCommonStockholders": 60.0},
        "balance": {"TotalAssets": 900.0, "CurrentLiabilities": 200.0, "TotalDebt": 300.0,
                    "CashAndCashEquivalents": 100.0, "OrdinarySharesNumber": 1_000_000.0},
        "cashflow": {"FreeCashFlow": 80.0},
    }
    for statement, valeurs in remplacements.items():
        champs[statement] = {**champs[statement], **valeurs}
    return _statements({
        dt.date(2025, 12, 31): champs,
        dt.date(2024, 12, 31): {"cashflow": {"FreeCashFlow": 70.0}},
        dt.date(2023, 12, 31): {"cashflow": {"FreeCashFlow": 60.0}},
        dt.date(2022, 12, 31): {"income": {"TotalRevenue": 800.0, "OperatingIncome": 60.0},
                                "balance": {"OrdinarySharesNumber": 950_000.0}},
    })


def test_multiples_de_valorisation():
    m = M.compute_stock_metrics(_inputs(statements=_valeur_complete(), prices=_prices([AS_OF], [1.0])))
    # Capitalisation = 1 € × 1 000 000 actions ; dette nette = 300 − 100 = 200.
    assert m.market_cap_eur == pytest.approx(1_000_000)
    assert m.ev_eur == pytest.approx(1_000_200)
    assert m.values["ev_ebit"] == pytest.approx(1_000_200 / 100.0)
    assert m.values["fcf_yield"] == pytest.approx(80.0 / 1_000_000)
    assert m.values["pe"] == pytest.approx(1_000_000 / 60.0)
    assert m.values["nd_ebitda"] == pytest.approx(200 / 150.0)
    assert m.values["interest_cov"] == pytest.approx(10.0)


def test_resultat_operationnel_negatif_donne_le_pire_score_pas_une_absence():
    etats = _valeur_complete(income={"OperatingIncome": -50.0})
    m = M.compute_stock_metrics(_inputs(statements=etats, prices=_prices([AS_OF], [1.0])))
    assert m.values["ev_ebit"] is None
    assert "ev_ebit" in m.worst                      # au pire rang, pas pénalisé comme absent
    assert "ev_ebit" not in m.missing_metrics
    assert "resultat_operationnel_non_positif" in m.flags


def test_perte_nette_donne_le_pire_score():
    etats = _valeur_complete(income={"NetIncomeCommonStockholders": -20.0, "NetIncome": -20.0})
    m = M.compute_stock_metrics(_inputs(statements=etats, prices=_prices([AS_OF], [1.0])))
    assert "pe" in m.worst and m.values["pe"] is None


def test_tresorerie_nette_classee_au_mieux():
    etats = _valeur_complete(balance={"TotalDebt": 50.0, "CashAndCashEquivalents": 300.0})
    m = M.compute_stock_metrics(_inputs(statements=etats, prices=_prices([AS_OF], [1.0])))
    assert m.net_debt == -250.0
    assert m.values["nd_ebitda"] < 0                 # un levier négatif se classe en tête


def test_absence_de_charge_d_interet_avec_tresorerie_nette():
    champs = {
        "income": {"TotalRevenue": 1000.0, "OperatingIncome": 100.0, "EBITDA": 150.0,
                   "NetIncome": 60.0, "NetIncomeCommonStockholders": 60.0},
        "balance": {"TotalAssets": 900.0, "CurrentLiabilities": 200.0, "TotalDebt": 10.0,
                    "CashAndCashEquivalents": 300.0, "OrdinarySharesNumber": 1_000_000.0},
        "cashflow": {"FreeCashFlow": 80.0},
    }
    m = M.compute_stock_metrics(_inputs(statements=_statements({dt.date(2025, 12, 31): champs})))
    assert m.values["interest_cov"] == math.inf
    assert "sans_charge_d_interet" in m.flags
    assert "income.FY0.InterestExpense" not in m.missing_fields


def test_endettement_sans_ebitda_positif_est_signale():
    etats = _valeur_complete(income={"EBITDA": -10.0})
    m = M.compute_stock_metrics(_inputs(statements=etats, prices=_prices([AS_OF], [1.0])))
    assert "nd_ebitda" in m.worst
    assert "ebitda_non_positif" in m.flags


def test_conversion_de_devise_pour_les_multiples():
    """Comptes en dollars, cours en euros : seuls les multiples convertissent."""
    etats = _valeur_complete()
    etats["currency"] = "USD"
    m = M.compute_stock_metrics(
        _inputs(statements=etats, prices=_prices([AS_OF], [1.0]), fx_rate=1.25)
    )
    assert m.values["fcf_yield"] == pytest.approx((80.0 / 1.25) / 1_000_000)
    # La marge, elle, ne dépend pas du change.
    assert m.values["op_margin"] == pytest.approx(0.10)


def test_taux_de_change_absent_rend_les_multiples_manquants():
    etats = _valeur_complete()
    etats["currency"] = "USD"
    m = M.compute_stock_metrics(
        _inputs(statements=etats, prices=_prices([AS_OF], [1.0]), fx_rate=None)
    )
    for nom in ("ev_ebit", "fcf_yield", "pe"):
        assert m.values[nom] is None and nom not in m.worst
    assert "taux_de_change_absent" in m.flags
    assert m.values["op_margin"] is not None      # les ratios sans dimension restent calculés


def test_ebitda_et_fcf_reconstitues():
    champs = {
        "income": {"TotalRevenue": 1000.0, "OperatingIncome": 100.0,
                   "DepreciationAndAmortizationInIncomeStatement": 50.0, "NetIncome": 60.0},
        "cashflow": {"OperatingCashFlow": 120.0, "CapitalExpenditure": -40.0},
        "balance": {"TotalDebt": 300.0, "CashAndCashEquivalents": 100.0},
    }
    m = M.compute_stock_metrics(_inputs(statements=_statements({dt.date(2025, 12, 31): champs})))
    assert m.ebitda == pytest.approx(150.0)
    assert m.values["cash_conversion"] == pytest.approx(80.0 / 60.0)
    assert "ebitda_reconstitue" in m.flags and "fcf_reconstitue" in m.flags


# ------------------------------------------------------------------------------ consensus


def test_consensus_variation_et_revisions():
    data = {"eps_0y_current": 6.6, "eps_0y_90d": 6.0, "eps_1y_current": 7.2, "eps_1y_90d": 7.2,
            "n_analysts_0y": 10, "up_30d_0y": 6, "down_30d_0y": 2,
            "n_analysts_1y": 10, "up_30d_1y": 4, "down_30d_1y": 0}
    m = M.compute_stock_metrics(_inputs(consensus=data))
    assert m.values["eps_rev_3m"] == pytest.approx(0.05)      # moyenne de +10 % et 0 %
    assert m.values["net_revisions"] == pytest.approx(8 / 20)
    assert m.consensus_incomplete is False


def test_consensus_absent_marque_la_composante_incomplete():
    m = M.compute_stock_metrics(_inputs(consensus=None))
    assert m.consensus_incomplete is True
    assert m.values["eps_rev_3m"] is None and m.values["net_revisions"] is None


def test_consensus_partiel_utilise_les_horizons_disponibles():
    data = {"eps_0y_current": 6.6, "eps_0y_90d": 6.0, "n_analysts_0y": 8,
            "up_30d_0y": 4, "down_30d_0y": 0}
    m = M.compute_stock_metrics(_inputs(consensus=data))
    assert m.values["eps_rev_3m"] == pytest.approx(0.10)
    assert m.values["net_revisions"] == pytest.approx(0.5)


def test_estimation_ancienne_quasi_nulle_ignoree():
    data = {"eps_0y_current": 3.4, "eps_0y_90d": 0.001, "n_analysts_0y": 0}
    m = M.compute_stock_metrics(_inputs(consensus=data))
    assert m.values["eps_rev_3m"] is None       # une variation contre zéro n'a pas de sens
    assert m.values["net_revisions"] is None
    assert m.consensus_incomplete is True


# ------------------------------------------------------------------------- champs requis


def test_liste_des_champs_requis():
    assert len(M.REQUIRED_FIELDS) == 19
    assert len(set(M.REQUIRED_FIELDS)) == 19


def test_valeur_complete_ne_manque_de_presque_rien():
    m = M.compute_stock_metrics(
        _inputs(statements=_valeur_complete(), prices=_serie_quotidienne(AS_OF, 500))
    )
    assert m.n_missing <= 1
    assert m.coverage_ratio > 0.9


def test_sans_etat_financier_tout_est_manquant():
    m = M.compute_stock_metrics(_inputs(prices=_serie_quotidienne(AS_OF, 500)))
    assert "aucun_etat_financier" in m.flags
    assert m.n_missing == len(M.REQUIRED_FIELDS) - 2   # les deux champs de cours sont présents


def test_etats_trop_anciens_rendent_les_fondamentaux_inutilisables():
    etats = _valeur_complete()
    etats["period_end"] = etats["period_end"].map(lambda d: dt.date(d.year - 2, d.month, d.day))
    m = M.compute_stock_metrics(_inputs(statements=etats, prices=_prices([AS_OF], [1.0])))
    assert "etats_trop_anciens" in m.flags
    assert m.values["op_margin"] is None


def test_flux_de_tresorerie_negatifs_trois_annees():
    etats = _statements({
        dt.date(2025, 12, 31): {"cashflow": {"FreeCashFlow": -10.0}},
        dt.date(2024, 12, 31): {"cashflow": {"FreeCashFlow": -20.0}},
        dt.date(2023, 12, 31): {"cashflow": {"FreeCashFlow": -5.0}},
    })
    assert M.compute_stock_metrics(_inputs(statements=etats)).fcf_negative_3y is True


def test_flux_manquant_rend_le_critere_non_evaluable():
    etats = _statements({
        dt.date(2025, 12, 31): {"cashflow": {"FreeCashFlow": -10.0}},
        dt.date(2024, 12, 31): {"cashflow": {"FreeCashFlow": -20.0}},
    })
    assert M.compute_stock_metrics(_inputs(statements=etats)).fcf_negative_3y is None
