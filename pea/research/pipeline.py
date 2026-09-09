"""Production d'un dossier d'investissement : extraction, débat, synthèse, vérification.

Le modèle lit, extrait, argumente et signale. Il ne choisit ni la valeur, ni la taille de
position, ni le moment. Un dossier dont un chiffre n'est pas rattaché à un fait fourni est
rejeté, et le rejet est enregistré au même titre qu'un succès.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from pydantic import ValidationError

from pea import db as db_module
from pea.research import context as ctx
from pea.research.llm import ClientLLM, LLMError, modeles
from pea.research.schema import Dossier, json_schema
from pea.research.verify import Rapport, verifier
from pea.screen.runs import git_sha

log = logging.getLogger(__name__)

PROMPTS = Path(__file__).resolve().parent / "prompts"
VERSIONS = {"extraction": "extraction-v1", "debat": "debat-v1", "synthese": "synthese-v1"}


@dataclass
class Resultat:
    dossier_id: str
    isin: str
    nom: str
    statut: str                 # valide | rejete | echec
    cout_eur: float
    duree_s: float
    conviction: int | None = None
    motifs: list[str] | None = None
    dossier: Dossier | None = None


def _identifiant_unique(con, travail, horodatage: dt.datetime) -> str:
    """Deux dossiers rédigés dans la même seconde restent distincts."""
    base = f"{travail.as_of:%Y-%m-%d}_{travail.isin}_{horodatage:%H%M%S}"
    identifiant, suffixe = base, 1
    while con.execute("SELECT 1 FROM dossiers WHERE dossier_id = ?", [identifiant]).fetchone():
        suffixe += 1
        identifiant = f"{base}-{suffixe}"
    return identifiant


def _prompt(nom: str) -> str:
    return (PROMPTS / f"{VERSIONS[nom]}.md").read_text(encoding="utf-8")


def produire(
    con,
    candidat,
    travail: ctx.DossierDeTravail,
    client: ClientLLM,
    *,
    run_id: str | None = None,
) -> Resultat:
    """Enchaîne les trois passes pour une valeur et enregistre le résultat."""
    debut = time.monotonic()
    horodatage = db_module.now_utc()
    dossier_id = _identifiant_unique(con, travail, horodatage)
    modele_eco, modele_fort = modeles()
    appels: list[tuple[str, object]] = []
    cout = 0.0
    faits = travail.rendu()

    try:
        extraction = client.json(
            modele_eco, _prompt("extraction"), f"{faits}\n\nProduis l'objet JSON demandé."
        )
        appels.append(("extraction", extraction))
        cout += extraction.cout_eur

        resume_extraction = json.dumps(extraction.contenu, ensure_ascii=False, indent=1)
        debat = client.json(
            modele_fort,
            _prompt("debat"),
            f"{faits}\n\nEXTRACTION FACTUELLE\n{resume_extraction}\n\n"
            "Produis l'objet JSON demandé.",
        )
        appels.append(("debat", debat))
        cout += debat.cout_eur

        synthese = client.json(
            modele_fort,
            _prompt("synthese"),
            f"{faits}\n\nEXTRACTION FACTUELLE\n{resume_extraction}\n\n"
            f"DÉBAT\n{json.dumps(debat.contenu, ensure_ascii=False, indent=1)}\n\n"
            f"Produis le dossier au format JSON. Le code ISIN est {travail.isin}.",
            schema=json_schema(),
        )
        appels.append(("synthese", synthese))
        cout += synthese.cout_eur
    except LLMError as exc:
        duree = round(time.monotonic() - debut, 1)
        _enregistrer(con, dossier_id, candidat, travail, None, None,
                     "echec", [str(exc)], cout, duree, run_id, horodatage, appels)
        return Resultat(dossier_id, travail.isin, travail.nom, "echec", cout, duree,
                        motifs=[str(exc)])

    duree = round(time.monotonic() - debut, 1)
    brut = dict(synthese.contenu)
    brut.setdefault("isin", travail.isin)

    try:
        dossier = Dossier.model_validate(brut)
    except ValidationError as exc:
        motifs = [f"{'.'.join(str(p) for p in e['loc'])} : {e['msg']}" for e in exc.errors()[:6]]
        _enregistrer(con, dossier_id, candidat, travail, None, brut,
                     "rejete", motifs, cout, duree, run_id, horodatage, appels)
        return Resultat(dossier_id, travail.isin, travail.nom, "rejete", cout, duree, motifs=motifs)

    suspects = extraction.contenu.get("textes_suspects") or []
    rapport = verifier(dossier, travail, suspects=suspects)
    statut = "valide" if rapport.valide else "rejete"
    _enregistrer(con, dossier_id, candidat, travail, dossier, brut, statut,
                 rapport.motifs(), cout, duree, run_id, horodatage, appels, rapport)
    return Resultat(
        dossier_id, travail.isin, travail.nom, statut, cout, duree,
        conviction=dossier.conviction, motifs=rapport.motifs(), dossier=dossier,
    )


def _enregistrer(
    con, dossier_id, candidat, travail, dossier, brut, statut, motifs, cout, duree,
    run_id, horodatage, appels, rapport: Rapport | None = None,
) -> None:
    """Écrit le dossier et le détail des appels. Une ligne enregistrée n'est jamais réécrite."""
    modele_eco, modele_fort = modeles()
    contenu = dossier.model_dump(mode="json") if dossier else brut
    if rapport is not None and contenu is not None:
        contenu = {**contenu, "_verification": {
            "valide": rapport.valide,
            "refs_inconnues": rapport.refs_inconnues,
            "nombres_non_sources": rapport.nombres_non_sources,
            "tournures_interdites": rapport.tournures_interdites,
            "textes_suspects": rapport.textes_suspects,
        }}
    ligne = {
        "dossier_id": dossier_id,
        "as_of": travail.as_of,
        "isin": travail.isin,
        "ticker": travail.ticker,
        "name": travail.nom,
        "run_id": run_id,
        "motif": getattr(candidat, "motif", "manuel"),
        "rank": getattr(candidat, "rank", None),
        "total_score": getattr(candidat, "total_score", None),
        "statut": statut,
        "motifs_rejet": "; ".join(motifs or []) or None,
        "conviction": dossier.conviction if dossier else None,
        "horizon_mois": dossier.horizon_mois if dossier else None,
        "contenu": json.dumps(contenu, ensure_ascii=False) if contenu is not None else None,
        "faits": json.dumps(
            [f.__dict__ for f in travail.faits], ensure_ascii=False, default=str
        ),
        "versions_prompts": json.dumps(VERSIONS, ensure_ascii=False),
        "modele_extraction": modele_eco,
        "modele_synthese": modele_fort,
        "cout_eur": round(cout, 6),
        "duree_s": duree,
        "git_sha": git_sha(),
        "genere_le_utc": horodatage,
    }
    db_module.insert_df(con, "dossiers", pd.DataFrame([ligne]), on_conflict="ignore")

    if appels:
        db_module.insert_df(
            con,
            "llm_calls",
            pd.DataFrame([
                {
                    "dossier_id": dossier_id,
                    "etape": etape,
                    "modele": reponse.modele,
                    "tokens_entree": reponse.tokens_entree,
                    "tokens_sortie": reponse.tokens_sortie,
                    "cout_eur": reponse.cout_eur,
                    "duree_s": reponse.duree_s,
                    "appele_le_utc": db_module.now_utc(),
                }
                for etape, reponse in appels
            ]),
        )


def charger_travail(con, candidat, as_of: dt.date, run_id: str) -> ctx.DossierDeTravail:
    """Rassemble tout ce que la base sait d'une valeur, à la date du classement."""
    from pea.screen.asof import cutoff_utc, statements_at

    ligne = con.execute(
        "SELECT * FROM scores WHERE run_id = ? AND isin = ?", [run_id, candidat.isin]
    ).df().iloc[0].to_dict()

    etats = pd.DataFrame()
    consensus = None
    articles: list[dict] = []
    if candidat.ticker:
        tous = statements_at(con, as_of, cutoff_utc(as_of))
        if not tous.empty:
            etats = tous[tous["ticker"] == candidat.ticker]
        ligne_consensus = con.execute(
            "SELECT * FROM consensus WHERE ticker = ? ORDER BY fetched_at_utc DESC LIMIT 1",
            [candidat.ticker],
        ).df()
        if not ligne_consensus.empty:
            consensus = ligne_consensus.iloc[0].to_dict()
        articles = con.execute(
            """
            SELECT titre, resume, url, publie_le FROM news
            WHERE ticker = ? AND (publie_le IS NULL OR publie_le <= ?)
            ORDER BY publie_le DESC NULLS LAST LIMIT 10
            """,
            [candidat.ticker, as_of],
        ).df().rename(columns={"publie_le": "date"}).to_dict("records")

    return ctx.construire(ligne, etats, consensus, articles, as_of)
