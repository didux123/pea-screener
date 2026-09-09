"""Export du classement au format que consomme l'interface web.

Le schéma est un contrat, décrit dans docs/ui/brief.md : les noms de champs sont fixes et
une donnée absente vaut null, jamais zéro.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import pandas as pd

from pea.screen.runs import load_run

# Nom interne de la métrique, nom exposé, unité, classée dans le secteur.
METRIC_EXPORT: tuple[tuple[str, str, str, bool], ...] = (
    ("rev_cagr_3y", "croissance_ca_3a", "pourcent", False),
    ("opinc_cagr_3y", "croissance_resultat_3a", "pourcent", False),
    ("op_margin", "marge_operationnelle", "pourcent", False),
    ("op_margin_trend_3y", "tendance_marge_3a", "points", False),
    ("roce", "rentabilite_capitaux", "pourcent", False),
    ("cash_conversion", "conversion_tresorerie", "ratio", False),
    ("mom_12_1", "momentum_12_1", "pourcent", False),
    ("mom_6_1", "momentum_6_1", "pourcent", False),
    ("dist_sma200", "ecart_moyenne_200j", "pourcent", False),
    ("ev_ebit", "ve_sur_resultat_op", "ratio", True),
    ("fcf_yield", "rendement_flux_libre", "pourcent", True),
    ("pe", "cours_sur_benefice", "ratio", True),
    ("nd_ebitda", "dette_nette_sur_ebitda", "ratio", False),
    ("interest_cov", "couverture_interets", "ratio", False),
    ("dilution_3y", "dilution_3a", "pourcent", False),
    ("eps_rev_3m", "revision_consensus_3m", "pourcent", False),
    ("net_revisions", "revisions_nettes", "ratio", False),
)

BLOCK_EXPORT = {
    "block_growth_quality": "croissance_qualite",
    "block_momentum": "momentum",
    "block_valuation": "valorisation",
    "block_balance": "bilan",
    "block_consensus": "consensus",
}


def _valeur(x):
    """Une donnée absente reste absente : null, jamais zéro."""
    if x is None:
        return None
    if isinstance(x, float | int) and not isinstance(x, bool):
        nombre = float(x)
        if math.isnan(nombre) or math.isinf(nombre):
            return None
        return nombre
    if pd.isna(x):
        return None
    return x


def _texte(x) -> str | None:
    valeur = _valeur(x)
    return None if valeur is None else str(valeur)


def _liste(x) -> list[str]:
    valeur = _valeur(x)
    return [part for part in str(valeur).split(";") if part] if valeur else []


def _date(x) -> str | None:
    if x is None or (not isinstance(x, str) and pd.isna(x)):
        return None
    return pd.Timestamp(x).date().isoformat()


def _horodatage(x) -> str | None:
    if x is None or (not isinstance(x, str) and pd.isna(x)):
        return None
    return pd.Timestamp(x).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_payload(run: dict, scores: pd.DataFrame, couverture: pd.DataFrame) -> dict:
    """Construit le document que lit l'interface."""
    retenues = scores[~scores["eliminated"].astype(bool)]
    ecartees = scores[scores["eliminated"].astype(bool)]

    universe = couverture[couverture["population"] == "universe"]
    total = int((universe["n_present"] + universe["n_missing"]).sum())
    couverture_moyenne = float(universe["n_present"].sum() / total) if total else 0.0

    raisons: dict[str, int] = {}
    for cellule in ecartees["elimination_reasons"].dropna():
        for raison in str(cellule).split(";"):
            if raison:
                raisons[raison] = raisons.get(raison, 0) + 1

    return {
        "genere_le": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": _date(run["as_of"]),
        "regime": run["mode"],
        "survivorship_complete": bool(run["survivorship_complete"]),
        "version_code": _texte(run.get("git_sha")),
        "derniere_information_utilisee": _date(run.get("max_info_date_used")),
        "derniere_recuperation_utilisee": _horodatage(run.get("max_fetched_at_used_utc")),
        "univers": {
            "examinees": int(run["n_universe"] or 0),
            "classees": int(run["n_scored"] or 0),
            "ecartees": int(run["n_eliminated"] or 0),
        },
        "couverture_moyenne": round(couverture_moyenne, 4),
        "valeurs": [_valeur_json(ligne) for ligne in retenues.to_dict("records")],
        "ecartees": [
            {
                "isin": ligne["isin"],
                "nom": _texte(ligne.get("name")),
                "secteur": _texte(ligne.get("sector")),
                "raisons": _liste(ligne.get("elimination_reasons")),
            }
            for ligne in ecartees.to_dict("records")
        ],
        "couverture_par_champ": [
            {"champ": ligne["field"], "present": int(ligne["n_present"]),
             "absent": int(ligne["n_missing"])}
            for ligne in universe.sort_values("n_present").to_dict("records")
        ],
        "raisons_exclusion": [
            {"raison": raison, "valeurs": n}
            for raison, n in sorted(raisons.items(), key=lambda item: -item[1])
        ],
    }


def _valeur_json(ligne: dict) -> dict:
    metriques = {}
    for interne, expose, unite, dans_secteur in METRIC_EXPORT:
        metriques[expose] = {
            "valeur": _valeur(ligne.get(f"m_{interne}")),
            "percentile": _valeur(ligne.get(f"s_{interne}")),
            "unite": unite,
        }
        if dans_secteur:
            metriques[expose]["classe_dans_le_secteur"] = True
    return {
        "rang": int(ligne["rank"]) if _valeur(ligne.get("rank")) is not None else None,
        "decile": int(ligne["decile"]) if _valeur(ligne.get("decile")) is not None else None,
        "isin": ligne["isin"],
        "ticker": _texte(ligne.get("ticker")),
        "nom": _texte(ligne.get("name")),
        "secteur": _texte(ligne.get("sector")),
        "industrie": _texte(ligne.get("industry")),
        "pays": _texte(ligne.get("country")),
        "place": _texte(ligne.get("mic")),
        "score": _valeur(ligne.get("total_score")),
        "blocs": {expose: _valeur(ligne.get(interne)) for interne, expose in BLOCK_EXPORT.items()},
        "consensus_incomplet": bool(ligne.get("consensus_incomplete")),
        "metriques": metriques,
        "cours": _valeur(ligne.get("price")),
        "date_cours": _date(ligne.get("price_date")),
        "capitalisation_eur": _valeur(ligne.get("market_cap_eur")),
        "valeur_entreprise_eur": _valeur(ligne.get("ev_eur")),
        "capitaux_echanges_3m_eur": _valeur(ligne.get("traded_value_3m_eur")),
        "exercice_reference": _date(ligne.get("fy0_period_end")),
        "anciennete_comptes_jours": (
            int(ligne["statement_age_days"])
            if _valeur(ligne.get("statement_age_days")) is not None else None
        ),
        "devise_comptes": _texte(ligne.get("statement_currency")),
        "couverture": _valeur(ligne.get("coverage_ratio")),
        "champs_manquants": _liste(ligne.get("missing_fields")),
        "metriques_penalisees": _liste(ligne.get("missing_metrics")),
        "drapeaux": _liste(ligne.get("flags")),
        "dossier": None,   # rempli au lot 2
    }


def write_export(con, run_id: str, cfg) -> Path:
    """Écrit data.json à côté des rapports du classement, et dans « latest »."""
    run, scores, couverture = load_run(con, run_id)
    payload = build_payload(run, scores, couverture)
    dossier = Path(cfg.reports_dir) / f"{run['as_of']:%Y-%m-%d}"
    dossier.mkdir(parents=True, exist_ok=True)
    chemin = dossier / "data.json"
    texte = json.dumps(payload, ensure_ascii=False, indent=1)
    chemin.write_text(texte, encoding="utf-8")
    dernier = Path(cfg.reports_dir) / "latest"
    dernier.mkdir(parents=True, exist_ok=True)
    (dernier / "data.json").write_text(texte, encoding="utf-8")
    return chemin
