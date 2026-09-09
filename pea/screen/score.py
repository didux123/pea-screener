"""Score composite 0-100 : filtres éliminatoires, percentiles, blocs, classement.

Les poids et les seuils viennent du cahier des charges et sont des constantes du code,
pas de la configuration : ils sont versionnés par le SHA git enregistré avec chaque run.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import pandas as pd

from pea.screen import asof as A
from pea.screen.metrics import METRIC_NAMES, REQUIRED_FIELDS, StockInputs, compute_stock_metrics
from pea.universe import EU_EEA_ISO2, load_overrides, pea_eligibility

# --- pondération du score (somme = 100) -------------------------------------------------
WEIGHTS: dict[str, int] = {
    "growth_quality": 35,
    "momentum": 25,
    "valuation": 20,
    "balance": 10,
    "consensus": 10,
}
BLOCKS: dict[str, tuple[str, ...]] = {
    "growth_quality": ("rev_cagr_3y", "opinc_cagr_3y", "op_margin", "op_margin_trend_3y",
                       "roce", "cash_conversion"),
    "momentum": ("mom_12_1", "mom_6_1", "dist_sma200"),
    "valuation": ("ev_ebit", "fcf_yield", "pe"),
    "balance": ("nd_ebitda", "interest_cov", "dilution_3y"),
    "consensus": ("eps_rev_3m", "net_revisions"),
}
# Métriques pour lesquelles une valeur élevée est favorable.
HIGHER_IS_BETTER: frozenset[str] = frozenset({
    "rev_cagr_3y", "opinc_cagr_3y", "op_margin", "op_margin_trend_3y", "roce", "cash_conversion",
    "mom_12_1", "mom_6_1", "dist_sma200", "fcf_yield", "interest_cov",
    "eps_rev_3m", "net_revisions",
})
# Valorisation : classée à l'intérieur du secteur, comme demandé.
WITHIN_SECTOR: frozenset[str] = frozenset({"ev_ebit", "fcf_yield", "pe"})

MISSING_METRIC_SCORE = 20.0      # une donnée absente pénalise, elle n'est jamais imputée
WORST_METRIC_SCORE = 0.0         # dénominateur non positif : information, pas absence
MIN_SECTOR_GROUP = 10            # en dessous, le percentile sectoriel n'a plus de sens
MAX_MISSING_RATIO = 0.40
MIN_TRADED_VALUE_EUR = 150_000.0
MAX_ND_EBITDA = 4.0
EXCLUDED_SECTORS = frozenset({"Financial Services", "Real Estate"})


@dataclass
class ScreenResult:
    as_of: dt.date
    mode: str
    survivorship_complete: bool
    scores: pd.DataFrame
    coverage: pd.DataFrame
    audit: A.Audit
    n_universe: int = 0
    n_scored: int = 0
    n_eliminated: int = 0
    flags: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------ filtres éliminatoires


def elimination_reasons(row: dict, metrics) -> list[str]:
    """Toutes les raisons d'écarter une valeur, évaluées sans court-circuit.

    On n'écarte que sur preuve positive, à deux exceptions près : l'éligibilité PEA et la
    liquidité doivent être établies, sinon la valeur n'est pas achetable en connaissance
    de cause.
    """
    reasons: list[str] = []

    if not row.get("yf_ticker"):
        reasons.append("symbole_non_resolu")

    eligible = row.get("pea_eligible")
    if eligible is False:
        reasons.append("non_eligible_pea")
    elif eligible is None:
        reasons.append(row.get("pea_reason") or "eligibilite_indeterminee")

    quote_type = row.get("quote_type")
    if quote_type and quote_type.upper() != "EQUITY":
        reasons.append("pas_une_action")

    sector = row.get("sector")
    if sector in EXCLUDED_SECTORS:
        reasons.append("secteur_exclu")

    traded = metrics.traded_value_3m_eur
    if traded is None:
        reasons.append("liquidite_inconnue")
    elif traded < MIN_TRADED_VALUE_EUR:
        reasons.append("illiquide")

    nd_ebitda = metrics.values.get("nd_ebitda")
    if "nd_ebitda" in metrics.worst:
        reasons.append("endettement_sans_ebitda")
    elif nd_ebitda is not None and nd_ebitda > MAX_ND_EBITDA:
        reasons.append("endettement_excessif")

    if metrics.fcf_negative_3y is True:
        reasons.append("flux_negatifs_trois_ans")

    if metrics.n_missing / len(REQUIRED_FIELDS) > MAX_MISSING_RATIO:
        reasons.append("donnees_insuffisantes")

    return reasons


def drop_duplicate_share_classes(frame: pd.DataFrame) -> pd.DataFrame:
    """Actions ordinaires et privilégiées d'une même société : on garde la plus liquide."""
    if frame.empty:
        return frame
    candidates = frame[~frame["eliminated"]].copy()
    if candidates.empty:
        return frame
    cle = candidates["long_name"].fillna(candidates["name"]).str.upper().str.strip()
    candidates = candidates.assign(_cle=cle + "|" + candidates["country"].fillna(""))
    candidates = candidates.sort_values("traded_value_3m_eur", ascending=False, na_position="last")
    doublons = candidates[candidates.duplicated(subset=["_cle"], keep="first")].index
    for index in doublons:
        frame.loc[index, "eliminated"] = True
        raisons = frame.loc[index, "elimination_reasons"]
        frame.loc[index, "elimination_reasons"] = ";".join(
            filter(None, [raisons, "classe_d_actions_doublon"])
        )
    return frame


# --------------------------------------------------------------------------------- percentiles


def percentile_scores(frame: pd.DataFrame) -> pd.DataFrame:
    """Transforme chaque métrique en un score 0-100 par rang, sur les valeurs retenues.

    Une métrique absente vaut 20 : c'est une pénalité explicite, listée par valeur, jamais
    une estimation. Un dénominateur non positif vaut 0 : le pire rang, mais la donnée
    existe et est comptée comme présente.
    """
    scored = frame[~frame["eliminated"]].copy()
    for metric in METRIC_NAMES:
        colonne = f"s_{metric}"
        frame[colonne] = pd.NA
        if scored.empty:
            continue
        if metric in WITHIN_SECTOR:
            valeurs = _rank_within_groups(scored, metric)
        else:
            valeurs = _rank_series(scored[f"m_{metric}"], metric)
        frame.loc[valeurs.index, colonne] = valeurs
        pires = scored.index[scored[f"w_{metric}"].fillna(False).astype(bool)]
        frame.loc[pires, colonne] = WORST_METRIC_SCORE
        manquantes = scored.index.difference(valeurs.index).difference(pires)
        frame.loc[manquantes, colonne] = MISSING_METRIC_SCORE
    return frame


def _rank_within_groups(scored: pd.DataFrame, metric: str) -> pd.Series:
    """Classement à l'intérieur du secteur.

    Un secteur qui compte moins de dix valeurs pourvues ne dit rien : ses membres sont
    alors classés contre l'univers entier, ce que le drapeau `repli_sectoriel` signale.
    """
    resultats = []
    petits_index = []
    for _, groupe in scored.groupby(scored["sector"].fillna("(inconnu)"), dropna=False):
        pourvues = groupe[f"m_{metric}"].notna() | groupe[f"w_{metric}"].fillna(False).astype(bool)
        if pourvues.sum() >= MIN_SECTOR_GROUP:
            resultats.append(_rank_series(groupe[f"m_{metric}"], metric))
        else:
            petits_index.extend(groupe.index)
    if petits_index:
        univers = _rank_series(scored[f"m_{metric}"], metric)
        repli = univers[univers.index.isin(petits_index)]
        if not repli.empty:
            resultats.append(repli)
    return pd.concat(resultats) if resultats else pd.Series(dtype="float64")


def _rank_series(series: pd.Series, metric: str) -> pd.Series:
    """Score 0-100 par rang moyen. Les ex æquo partagent le même score."""
    valeurs = series.dropna().astype("float64")
    if valeurs.empty:
        return pd.Series(dtype="float64")
    if len(valeurs) == 1:
        return pd.Series([50.0], index=valeurs.index)
    croissant = metric in HIGHER_IS_BETTER
    rangs = valeurs.rank(method="average", ascending=croissant)
    return 100.0 * (rangs - 0.5) / len(valeurs)


def block_scores(frame: pd.DataFrame) -> pd.DataFrame:
    """Un bloc vaut la moyenne de ses métriques ; le total, la somme pondérée des blocs."""
    total = pd.Series(0.0, index=frame.index)
    for bloc, metriques in BLOCKS.items():
        colonnes = [f"s_{m}" for m in metriques]
        valeurs = frame[colonnes].apply(pd.to_numeric, errors="coerce")
        moyenne = valeurs.mean(axis=1)
        frame[f"block_{bloc}"] = moyenne
        total += moyenne.fillna(0.0) * WEIGHTS[bloc] / 100.0
    frame["total_score"] = total.where(~frame["eliminated"])
    frame.loc[frame["eliminated"], [f"block_{b}" for b in BLOCKS]] = pd.NA
    return frame


def total_and_rank(frame: pd.DataFrame) -> pd.DataFrame:
    """Rang décroissant, égalité départagée par l'ISIN pour que deux exécutions concordent."""
    frame["rank"] = pd.NA
    frame["decile"] = pd.NA
    retenues = frame[~frame["eliminated"]]
    if retenues.empty:
        return frame
    ordonne = retenues.sort_values(["total_score", "isin"], ascending=[False, True])
    rangs = range(1, len(ordonne) + 1)
    frame.loc[ordonne.index, "rank"] = list(rangs)
    n = len(ordonne)
    frame.loc[ordonne.index, "decile"] = [
        min(10, math.ceil(10 * rang / n)) for rang in range(1, n + 1)
    ]
    return frame


# ------------------------------------------------------------------------ assemblage du run


def _fx_lookup(pit: A.PITData) -> dict[str, float]:
    if pit.fx.empty:
        return {}
    return dict(zip(pit.fx["quote_ccy"], pit.fx["rate"].astype("float64"), strict=True))


def _rate_for(currency: str | None, taux: dict[str, float]) -> float | None:
    if not currency:
        return None
    if currency.upper() == "EUR":
        return 1.0
    return taux.get(currency.upper())


def run_screen(pit: A.PITData, *, overrides_path=None) -> ScreenResult:
    """Calcule le classement à partir d'une photographie de la base. Aucune écriture."""
    overrides = load_overrides(overrides_path) if overrides_path else {}
    taux = _fx_lookup(pit)
    descripteurs = pit.descriptors.set_index("ticker").to_dict("index") if not pit.descriptors.empty else {}
    consensus = pit.consensus.set_index("ticker").to_dict("index") if not pit.consensus.empty else {}
    etats = pit.statements.groupby("ticker") if not pit.statements.empty else None
    cours = pit.prices.groupby("ticker") if not pit.prices.empty else None

    lignes: list[dict] = []
    couverture_champs: dict[str, int] = {champ: 0 for champ in REQUIRED_FIELDS}
    drapeaux_run: list[str] = []

    for valeur in pit.universe.to_dict("records"):
        ticker = valeur.get("yf_ticker")
        descripteur = descripteurs.get(ticker, {}) if ticker else {}
        etats_valeur = _group(etats, ticker)
        cours_valeur = _group(cours, ticker)
        splits = cours_valeur[cours_valeur["split_ratio"] > 0] if not cours_valeur.empty else cours_valeur

        devise_etats = _statement_currency(etats_valeur) or descripteur.get("financial_currency")
        metrics = compute_stock_metrics(
            StockInputs(
                ticker=ticker or valeur["isin"],
                as_of=pit.as_of,
                statements=etats_valeur,
                prices=cours_valeur,
                splits=splits,
                descriptor=descripteur,
                consensus=consensus.get(ticker) if ticker else None,
                fx_rate=_rate_for(devise_etats, taux),
                quote_fx_rate=_rate_for(
                    descripteur.get("quote_currency") or valeur.get("quote_currency") or "EUR", taux
                ) or 1.0,
            )
        )

        eligible, raison = pea_eligibility(
            valeur.get("isin_country"), descripteur.get("country"), overrides, isin=valeur["isin"]
        )
        contexte = {
            **valeur,
            "pea_eligible": eligible,
            "pea_reason": raison,
            "sector": descripteur.get("sector"),
            "industry": descripteur.get("industry"),
            "country": descripteur.get("country"),
            "quote_type": descripteur.get("quote_type"),
            "long_name": descripteur.get("long_name"),
        }
        raisons = elimination_reasons(contexte, metrics)
        if descripteur.get("from_later_fetch"):
            metrics.flags.append("descripteur_posterieur")

        for champ in REQUIRED_FIELDS:
            if champ not in metrics.missing_fields:
                couverture_champs[champ] += 1

        lignes.append(_row(pit, contexte, metrics, raisons))

    frame = pd.DataFrame(lignes)
    if frame.empty:
        return ScreenResult(pit.as_of, pit.mode, pit.survivorship_complete, frame,
                            pd.DataFrame(columns=["population", "field", "n_present", "n_missing"]),
                            pit.audit)

    frame = drop_duplicate_share_classes(frame)
    frame = percentile_scores(frame)
    frame = block_scores(frame)
    frame = total_and_rank(frame)
    frame = frame.sort_values(["rank", "isin"], na_position="last").reset_index(drop=True)

    if pit.mode == "reconstructed":
        drapeaux_run.append("classement_reconstitue")
    if pit.consensus.empty:
        drapeaux_run.append("aucun_consensus_disponible")

    couverture = _coverage_frame(frame, couverture_champs, len(pit.universe))
    return ScreenResult(
        as_of=pit.as_of,
        mode=pit.mode,
        survivorship_complete=pit.survivorship_complete,
        scores=frame,
        coverage=couverture,
        audit=pit.audit,
        n_universe=len(frame),
        n_scored=int((~frame["eliminated"]).sum()),
        n_eliminated=int(frame["eliminated"].sum()),
        flags=drapeaux_run,
    )


def _group(grouped, ticker):
    vide = pd.DataFrame(
        columns=["ticker", "date", "close", "volume", "dividend", "split_ratio",
                 "period_end", "statement", "field", "value", "currency"]
    )
    if grouped is None or not ticker or ticker not in grouped.groups:
        return vide
    return grouped.get_group(ticker)


def _statement_currency(etats: pd.DataFrame) -> str | None:
    if etats.empty or "currency" not in etats.columns:
        return None
    valeurs = etats["currency"].dropna().unique()
    return valeurs[0] if len(valeurs) else None


def _row(pit, contexte, metrics, raisons) -> dict:
    ligne = {
        "as_of": pit.as_of,
        "isin": contexte["isin"],
        "ticker": contexte.get("yf_ticker"),
        "name": contexte.get("name"),
        "long_name": contexte.get("long_name"),
        "mic": contexte.get("mic"),
        "sector": contexte.get("sector"),
        "industry": contexte.get("industry"),
        "country": contexte.get("country"),
        "eliminated": bool(raisons),
        "elimination_reasons": ";".join(raisons),
        "consensus_incomplete": metrics.consensus_incomplete,
        "price": metrics.price,
        "price_date": metrics.price_date,
        "market_cap_eur": metrics.market_cap_eur,
        "ev_eur": metrics.ev_eur,
        "traded_value_3m_eur": metrics.traded_value_3m_eur,
        "fy0_period_end": metrics.fy0_period_end,
        "statement_age_days": metrics.statement_age_days,
        "statement_currency": metrics.statement_currency,
        "fx_rate_used": metrics.fx_rate_used,
        "n_required": len(REQUIRED_FIELDS),
        "n_missing": metrics.n_missing,
        "missing_fields": ";".join(metrics.missing_fields),
        "missing_metrics": ";".join(metrics.missing_metrics),
        "coverage_ratio": metrics.coverage_ratio,
        "flags": ";".join(metrics.flags),
    }
    for metric in METRIC_NAMES:
        valeur = metrics.values.get(metric)
        ligne[f"m_{metric}"] = None if valeur is None or valeur in (math.inf, -math.inf) else valeur
        ligne[f"w_{metric}"] = metric in metrics.worst
        if valeur == math.inf:
            ligne[f"m_{metric}"] = math.inf   # sentinelle conservée pour le classement
    return ligne


def _coverage_frame(frame: pd.DataFrame, champs: dict[str, int], n_univers: int) -> pd.DataFrame:
    lignes = []
    retenues = frame[~frame["eliminated"]]
    for champ, present in champs.items():
        lignes.append({
            "population": "universe", "field": champ,
            "n_present": present, "n_missing": n_univers - present,
        })
    for metric in METRIC_NAMES:
        present = int(retenues[f"m_{metric}"].notna().sum() + retenues[f"w_{metric}"].fillna(False).sum())
        lignes.append({
            "population": "scored", "field": metric,
            "n_present": present, "n_missing": len(retenues) - present,
        })
    return pd.DataFrame(lignes, columns=["population", "field", "n_present", "n_missing"])
