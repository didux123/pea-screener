"""Schéma du dossier d'investissement. C'est un contrat, validé avant tout stockage.

Chaque affirmation chiffrée doit citer les faits sur lesquels elle s'appuie. Un champ que
le modèle ne peut pas étayer vaut « non disponible » : il n'invente jamais.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

NON_DISPONIBLE = "non disponible"

# Un identifiant de fait, tel que fourni au modèle dans le dossier de travail.
RefFait = Annotated[str, Field(pattern=r"^[FSN]\d{1,3}$")]


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _texte_ou_non_disponible(minimum: int):
    """Un champ rédigé, ou l'aveu explicite que l'information manque.

    La consigne donnée au modèle est d'écrire « non disponible » plutôt que d'inventer. Le
    schéma doit donc accepter cette réponse, sans quoi la consigne serait contradictoire.
    """

    def valider(valeur: str) -> str:
        nettoye = valeur.strip()
        if nettoye.lower().startswith(NON_DISPONIBLE):
            return NON_DISPONIBLE
        if len(nettoye) < minimum:
            raise ValueError(f"au moins {minimum} caractères, ou « {NON_DISPONIBLE} »")
        return nettoye

    return valider


TexteLong = Annotated[str, AfterValidator(_texte_ou_non_disponible(80)), Field(max_length=900)]
TexteMoyen = Annotated[str, AfterValidator(_texte_ou_non_disponible(40)), Field(max_length=900)]


class Source(Base):
    """Une source citée. Les faits internes portent la date de leur exercice."""

    ref: RefFait
    titre: str = Field(min_length=3, max_length=300)
    url: str | None = None
    date: dt.date | None = None
    origine: Literal["donnees_internes", "presse", "consensus"]


class Risque(Base):
    gravite: Literal[1, 2, 3] = Field(description="1 est le plus grave")
    titre: str = Field(min_length=5, max_length=160)
    explication: str = Field(min_length=20, max_length=900)
    refs: list[RefFait] = Field(default_factory=list)


class Catalyseur(Base):
    date: dt.date | None = None
    horizon: Literal["0-6 mois", "6-12 mois", "12-24 mois"]
    evenement: str = Field(min_length=10, max_length=300)
    refs: list[RefFait] = Field(default_factory=list)


class CritereInvalidation(Base):
    """Le champ le plus important : un événement observable qui rend la thèse fausse.

    Il doit être vérifiable sans jugement, à partir d'une publication ou d'un cours.
    """

    critere: str = Field(min_length=15, max_length=300)
    mesure: str = Field(min_length=5, max_length=160, description="ce qu'on observe pour trancher")
    seuil: str = Field(min_length=1, max_length=120)
    echeance: Literal["prochaine publication", "6 mois", "12 mois", "24 mois"]


class HausseJustifiee(Base):
    verdict: Literal["fondamentaux", "recit", "mixte", "non disponible"]
    explication: str = Field(min_length=20, max_length=900)
    refs: list[RefFait] = Field(default_factory=list)


class Fourchette(Base):
    basse: float | None = None
    haute: float | None = None
    devise: str | None = None
    methode: str = Field(min_length=10, max_length=300)
    refs: list[RefFait] = Field(default_factory=list)


class Debat(Base):
    """Les deux plaidoiries, conservées telles quelles : c'est le raisonnement archivé."""

    haussier: str = Field(min_length=100, max_length=4000)
    baissier: str = Field(min_length=100, max_length=4000)
    refs_haussier: list[RefFait] = Field(default_factory=list)
    refs_baissier: list[RefFait] = Field(default_factory=list)


class Dossier(Base):
    """Le dossier d'investissement. Aucune taille de position, aucun ordre, aucun prix d'achat."""

    isin: str = Field(pattern=r"^[A-Z]{2}[A-Z0-9]{9}\d$")
    activite: TexteLong = Field(description="trois phrases")
    origine_du_chiffre_affaires: TexteMoyen
    moteurs_de_croissance: list[str] = Field(min_length=1, max_length=5)
    avantage_concurrentiel: TexteMoyen
    ce_qui_le_ferait_disparaitre: TexteMoyen
    risques: list[Risque] = Field(min_length=3, max_length=3)
    hausse_justifiee: HausseJustifiee
    signes_alerte: list[str] = Field(default_factory=list, max_length=8)
    catalyseurs: list[Catalyseur] = Field(default_factory=list, max_length=6)
    conviction: int = Field(ge=0, le=100)
    horizon_mois: Literal[6, 12, 18, 24, 36]
    fourchette_valorisation: Fourchette
    criteres_invalidation: list[CritereInvalidation] = Field(min_length=2, max_length=3)
    debat: Debat
    sources: list[Source] = Field(min_length=1)

    @field_validator("moteurs_de_croissance", "signes_alerte")
    @classmethod
    def _pas_de_ligne_vide(cls, valeurs: list[str]) -> list[str]:
        for texte in valeurs:
            if len(texte.strip()) < 10:
                raise ValueError("chaque élément doit être une phrase, pas un mot isolé")
        return valeurs

    @field_validator("sources")
    @classmethod
    def _refs_uniques(cls, sources: list[Source]) -> list[Source]:
        refs = [s.ref for s in sources]
        if len(refs) != len(set(refs)):
            raise ValueError("deux sources portent la même référence")
        return sources

    def refs_citees(self) -> set[str]:
        """Toutes les références invoquées dans le dossier."""
        refs: set[str] = set()
        for risque in self.risques:
            refs.update(risque.refs)
        for catalyseur in self.catalyseurs:
            refs.update(catalyseur.refs)
        refs.update(self.hausse_justifiee.refs)
        refs.update(self.fourchette_valorisation.refs)
        refs.update(self.debat.refs_haussier)
        refs.update(self.debat.refs_baissier)
        return refs


def json_schema() -> dict:
    """Schéma transmis au modèle pour qu'il produise directement la bonne forme."""
    return Dossier.model_json_schema()
