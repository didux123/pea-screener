"""Score composite : percentiles, pénalité d'absence, filtres, classement."""

from __future__ import annotations

import datetime as dt
import math

import pandas as pd

from pea.screen import score as S
from pea.screen.metrics import METRIC_NAMES, REQUIRED_FIELDS, StockMetrics

AS_OF = dt.date(2026, 3, 15)


def _metrics(**kwargs) -> StockMetrics:
    m = StockMetrics(values={name: None for name in METRIC_NAMES})
    m.traded_value_3m_eur = kwargs.pop("traded_value_3m_eur", 1_000_000.0)
    for key, value in kwargs.items():
        if key in METRIC_NAMES:
            m.values[key] = value
        else:
            setattr(m, key, value)
    return m


def _frame(lignes: list[dict]) -> pd.DataFrame:
    """Construit un tableau de classement minimal à partir de valeurs de métriques."""
    rows = []
    for i, ligne in enumerate(lignes):
        row = {
            "isin": ligne.get("isin", f"FR000000{i:04d}"),
            "name": ligne.get("name", f"V{i}"),
            "long_name": ligne.get("long_name", f"Valeur {i}"),
            "sector": ligne.get("sector", "Technology"),
            "country": ligne.get("country", "France"),
            "eliminated": ligne.get("eliminated", False),
            "elimination_reasons": ligne.get("elimination_reasons", ""),
            "traded_value_3m_eur": ligne.get("traded_value_3m_eur", 1e6),
        }
        for metric in METRIC_NAMES:
            row[f"m_{metric}"] = ligne.get(metric)
            row[f"w_{metric}"] = metric in ligne.get("worst", set())
        rows.append(row)
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------ pondération


def test_les_poids_font_cent():
    assert sum(S.WEIGHTS.values()) == 100


def test_les_blocs_couvrent_toutes_les_metriques():
    dans_les_blocs = [m for metriques in S.BLOCKS.values() for m in metriques]
    assert sorted(dans_les_blocs) == sorted(METRIC_NAMES)
    assert len(dans_les_blocs) == len(set(dans_les_blocs))


# ------------------------------------------------------------------------------ percentiles


def test_rangs_repartis_regulierement():
    frame = S.percentile_scores(_frame([{"roce": v} for v in (0.1, 0.2, 0.3, 0.4, 0.5)]))
    assert sorted(frame["s_roce"]) == [10.0, 30.0, 50.0, 70.0, 90.0]


def test_ex_aequo_partagent_le_meme_score():
    frame = S.percentile_scores(_frame([{"roce": v} for v in (0.1, 0.2, 0.2, 0.4)]))
    scores = list(frame["s_roce"])
    assert scores[1] == scores[2]


def test_sens_inverse_pour_une_metrique_ou_le_faible_est_bon():
    frame = S.percentile_scores(_frame([{"nd_ebitda": v} for v in (0.5, 1.0, 3.0)]))
    # Le moins endetté obtient le meilleur score.
    assert frame.loc[0, "s_nd_ebitda"] > frame.loc[2, "s_nd_ebitda"]


def test_valeur_unique_recoit_le_score_median():
    frame = S.percentile_scores(_frame([{"roce": 0.2}]))
    assert frame.loc[0, "s_roce"] == 50.0


def test_metrique_absente_penalisee_et_jamais_imputee():
    frame = S.percentile_scores(_frame([{"roce": 0.1}, {"roce": 0.5}, {}]))
    assert frame.loc[2, "s_roce"] == S.MISSING_METRIC_SCORE == 20.0
    # La pénalité n'est pas une médiane déguisée : elle ne dépend pas des autres valeurs.
    autre = S.percentile_scores(_frame([{"roce": 10.0}, {"roce": 50.0}, {}]))
    assert autre.loc[2, "s_roce"] == 20.0


def test_denominateur_non_positif_recoit_le_pire_score():
    frame = S.percentile_scores(
        _frame([{"ev_ebit": 10.0}, {"ev_ebit": 20.0}, {"worst": {"ev_ebit"}}])
    )
    assert frame.loc[2, "s_ev_ebit"] == S.WORST_METRIC_SCORE == 0.0
    assert frame.loc[2, "s_ev_ebit"] < frame["s_ev_ebit"].drop(2).min()


def test_sentinelle_infinie_classee_au_mieux():
    frame = S.percentile_scores(
        _frame([{"interest_cov": 3.0}, {"interest_cov": 12.0}, {"interest_cov": math.inf}])
    )
    assert frame.loc[2, "s_interest_cov"] == frame["s_interest_cov"].max()


def test_valorisation_classee_dans_le_secteur():
    lignes = (
        [{"sector": "Technology", "ev_ebit": v} for v in range(5, 25)]
        + [{"sector": "Energy", "ev_ebit": v} for v in range(100, 120)]
    )
    frame = S.percentile_scores(_frame(lignes))
    tech = frame[frame["sector"] == "Technology"]
    energie = frame[frame["sector"] == "Energy"]
    # Une valorisation de 100 est chère dans l'absolu mais banale dans son secteur.
    assert energie["s_ev_ebit"].max() > 80
    assert tech["s_ev_ebit"].max() > 80


def test_secteur_trop_petit_bascule_sur_l_univers():
    lignes = (
        [{"sector": "Technology", "ev_ebit": v} for v in range(5, 25)]
        + [{"sector": "Utilities", "ev_ebit": 1.0}]   # un seul membre
    )
    frame = S.percentile_scores(_frame(lignes))
    utilities = frame[frame["sector"] == "Utilities"].iloc[0]
    # Classée contre tout l'univers, sa valorisation très basse la met en tête.
    assert utilities["s_ev_ebit"] > 90


def test_percentiles_calcules_hors_valeurs_eliminees():
    sans = S.percentile_scores(_frame([{"roce": 0.1}, {"roce": 0.2}, {"roce": 0.3}]))
    avec = S.percentile_scores(
        _frame([{"roce": 0.1}, {"roce": 0.2}, {"roce": 0.3},
                {"roce": 99.0, "eliminated": True, "elimination_reasons": "illiquide"}])
    )
    assert list(sans["s_roce"]) == list(avec["s_roce"][:3])


# ---------------------------------------------------------------------------------- blocs


def test_blocs_et_total_dans_les_bornes():
    lignes = [{m: float(i) for m in METRIC_NAMES} for i in range(1, 11)]
    frame = S.total_and_rank(S.block_scores(S.percentile_scores(_frame(lignes))))
    assert frame["total_score"].between(0, 100).all()
    for bloc in S.BLOCKS:
        assert frame[f"block_{bloc}"].between(0, 100).all()


def test_valeur_eliminee_sans_score():
    frame = S.total_and_rank(S.block_scores(S.percentile_scores(_frame([
        {"roce": 0.3}, {"roce": 0.1, "eliminated": True, "elimination_reasons": "illiquide"},
    ]))))
    assert pd.isna(frame.loc[1, "total_score"])
    assert pd.isna(frame.loc[1, "rank"])
    assert frame.loc[1, "elimination_reasons"] == "illiquide"


def test_rang_et_decile():
    lignes = [{"roce": float(i)} for i in range(20)]
    frame = S.total_and_rank(S.block_scores(S.percentile_scores(_frame(lignes))))
    retenues = frame[~frame["eliminated"]]
    assert sorted(retenues["rank"]) == list(range(1, 21))
    meilleure = retenues.loc[retenues["rank"] == 1].iloc[0]
    assert meilleure["m_roce"] == 19.0        # la plus rentable est première
    assert meilleure["decile"] == 1
    assert retenues.loc[retenues["rank"] == 20].iloc[0]["decile"] == 10


def test_egalite_departagee_par_l_isin():
    frame = S.total_and_rank(S.block_scores(S.percentile_scores(_frame([
        {"isin": "FR0000000002", "roce": 0.2}, {"isin": "FR0000000001", "roce": 0.2},
    ]))))
    premier = frame[frame["rank"] == 1].iloc[0]
    assert premier["isin"] == "FR0000000001"


# ---------------------------------------------------------------------- filtres éliminatoires


def _contexte(**kwargs):
    base = {"yf_ticker": "T.PA", "pea_eligible": True, "pea_reason": "ok",
            "quote_type": "EQUITY", "sector": "Technology"}
    return {**base, **kwargs}


def test_aucune_raison_pour_une_valeur_saine():
    assert S.elimination_reasons(_contexte(), _metrics()) == []


def test_non_eligible_au_pea():
    assert "non_eligible_pea" in S.elimination_reasons(_contexte(pea_eligible=False), _metrics())


def test_eligibilite_indeterminee_ecarte():
    raisons = S.elimination_reasons(
        _contexte(pea_eligible=None, pea_reason="pea_conflict: isin NL vs yahoo CH"), _metrics()
    )
    assert any("pea_conflict" in r for r in raisons)


def test_secteur_exclu():
    assert "secteur_exclu" in S.elimination_reasons(
        _contexte(sector="Financial Services"), _metrics()
    )
    assert "secteur_exclu" in S.elimination_reasons(_contexte(sector="Real Estate"), _metrics())


def test_secteur_inconnu_ne_suffit_pas_a_ecarter():
    assert S.elimination_reasons(_contexte(sector=None), _metrics()) == []


def test_liquidite_insuffisante_et_inconnue():
    assert "illiquide" in S.elimination_reasons(
        _contexte(), _metrics(traded_value_3m_eur=100_000.0)
    )
    assert "liquidite_inconnue" in S.elimination_reasons(
        _contexte(), _metrics(traded_value_3m_eur=None)
    )


def test_endettement_excessif_mais_inconnu_n_ecarte_pas():
    assert "endettement_excessif" in S.elimination_reasons(
        _contexte(), _metrics(nd_ebitda=4.5)
    )
    assert S.elimination_reasons(_contexte(), _metrics(nd_ebitda=None)) == []
    assert "endettement_sans_ebitda" in S.elimination_reasons(
        _contexte(), _metrics(worst={"nd_ebitda"})
    )


def test_flux_negatifs_trois_ans():
    assert "flux_negatifs_trois_ans" in S.elimination_reasons(
        _contexte(), _metrics(fcf_negative_3y=True)
    )
    assert S.elimination_reasons(_contexte(), _metrics(fcf_negative_3y=None)) == []


def test_regle_des_quarante_pour_cent():
    sept = _metrics(missing_fields=list(REQUIRED_FIELDS[:7]))
    huit = _metrics(missing_fields=list(REQUIRED_FIELDS[:8]))
    assert "donnees_insuffisantes" not in S.elimination_reasons(_contexte(), sept)
    assert "donnees_insuffisantes" in S.elimination_reasons(_contexte(), huit)


def test_toutes_les_raisons_sont_cumulees():
    raisons = S.elimination_reasons(
        _contexte(pea_eligible=False, sector="Real Estate"),
        _metrics(traded_value_3m_eur=1000.0, nd_ebitda=9.0, fcf_negative_3y=True),
    )
    assert {"non_eligible_pea", "secteur_exclu", "illiquide",
            "endettement_excessif", "flux_negatifs_trois_ans"} <= set(raisons)


def test_symbole_non_resolu():
    assert "symbole_non_resolu" in S.elimination_reasons(_contexte(yf_ticker=None), _metrics())


def test_classe_d_actions_doublon_garde_la_plus_liquide():
    frame = _frame([
        {"isin": "DE0000000001", "long_name": "Sartorius AG", "traded_value_3m_eur": 5e6},
        {"isin": "DE0000000002", "long_name": "Sartorius AG", "traded_value_3m_eur": 1e5},
        {"isin": "DE0000000003", "long_name": "Autre AG", "traded_value_3m_eur": 2e6},
    ])
    resultat = S.drop_duplicate_share_classes(frame)
    assert not resultat.loc[0, "eliminated"]
    assert resultat.loc[1, "eliminated"]
    assert "classe_d_actions_doublon" in resultat.loc[1, "elimination_reasons"]
    assert not resultat.loc[2, "eliminated"]
