"""Appels au modèle de langage via LiteLLM, avec journal des coûts.

Deux modèles : un économique pour l'extraction, un plus fort pour le débat et la synthèse.
Le fournisseur se choisit par variable d'environnement, comme demandé.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Modèles par défaut, par fournisseur. Le premier est l'économique, le second le plus fort.
MODELES = {
    "gemini": ("gemini/gemini-2.5-flash", "gemini/gemini-2.5-pro"),
    "mistral": ("mistral/mistral-small-latest", "mistral/mistral-large-latest"),
}
CLE_ATTENDUE = {"gemini": "GEMINI_API_KEY", "mistral": "MISTRAL_API_KEY"}


class LLMError(RuntimeError):
    """Le modèle n'a pas répondu, ou pas dans la forme attendue."""


@dataclass
class Reponse:
    contenu: dict
    modele: str
    tokens_entree: int
    tokens_sortie: int
    cout_eur: float
    duree_s: float


def fournisseur() -> str:
    nom = (os.environ.get("LLM_PROVIDER") or "gemini").strip().lower()
    if nom not in MODELES:
        raise LLMError(f"fournisseur inconnu : {nom} (attendus : {', '.join(MODELES)})")
    return nom


def modeles() -> tuple[str, str]:
    """(modèle économique, modèle fort), surchargeables par variable d'environnement."""
    economique, fort = MODELES[fournisseur()]
    return (
        os.environ.get("LLM_MODEL_EXTRACTION") or economique,
        os.environ.get("LLM_MODEL_SYNTHESE") or fort,
    )


def cle_presente() -> bool:
    return bool(os.environ.get(CLE_ATTENDUE[fournisseur()]))


class ClientLLM:
    """Enveloppe LiteLLM. Sortie JSON exigée, coût mesuré, réponse tronquée refusée."""

    def __init__(self, *, timeout: int = 180, tentatives: int = 3, taux_usd_eur: float = 0.92):
        self.timeout = timeout
        self.tentatives = tentatives
        self.taux_usd_eur = taux_usd_eur

    def json(self, modele: str, consigne: str, message: str, *, schema: dict | None = None) -> Reponse:
        """Demande une réponse JSON. Lève LLMError après épuisement des tentatives."""
        import litellm

        derniere: Exception | None = None
        for essai in range(self.tentatives):
            debut = time.monotonic()
            try:
                parametres = {
                    "model": modele,
                    "messages": [
                        {"role": "system", "content": consigne},
                        {"role": "user", "content": message},
                    ],
                    "temperature": 0.2,
                    "timeout": self.timeout,
                    "response_format": {"type": "json_object"},
                }
                if schema:
                    parametres["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {"name": "dossier", "schema": schema, "strict": False},
                    }
                brut = litellm.completion(**parametres)
            except Exception as exc:
                derniere = exc
                attente = 2 ** essai
                log.warning("appel au modèle en échec (%s), nouvelle tentative dans %ds",
                            type(exc).__name__, attente)
                time.sleep(attente)
                continue

            duree = time.monotonic() - debut
            choix = brut.choices[0]
            if getattr(choix, "finish_reason", None) == "length":
                derniere = LLMError("réponse tronquée par la limite de longueur")
                continue
            texte = choix.message.content or ""
            try:
                contenu = _charge_json(texte)
            except ValueError as exc:
                derniere = exc
                log.warning("réponse non analysable, nouvelle tentative")
                continue

            usage = getattr(brut, "usage", None)
            return Reponse(
                contenu=contenu,
                modele=modele,
                tokens_entree=int(getattr(usage, "prompt_tokens", 0) or 0),
                tokens_sortie=int(getattr(usage, "completion_tokens", 0) or 0),
                cout_eur=self._cout(brut),
                duree_s=round(duree, 2),
            )
        raise LLMError(f"{modele} : {derniere}")

    def _cout(self, brut) -> float:
        try:
            import litellm

            usd = litellm.completion_cost(completion_response=brut) or 0.0
        except Exception:
            return 0.0
        return round(float(usd) * self.taux_usd_eur, 6)


def _charge_json(texte: str) -> dict:
    """Analyse la réponse, en tolérant un bloc de code autour du JSON."""
    texte = texte.strip()
    if texte.startswith("```"):
        texte = re.sub(r"^```[a-zA-Z]*\n", "", texte)
        texte = re.sub(r"\n```$", "", texte.strip())
    try:
        contenu = json.loads(texte)
    except json.JSONDecodeError as exc:
        debut, fin = texte.find("{"), texte.rfind("}")
        if debut == -1 or fin <= debut:
            raise ValueError(f"réponse sans objet JSON : {texte[:200]}") from exc
        contenu = json.loads(texte[debut : fin + 1])
    if not isinstance(contenu, dict):
        raise ValueError("la réponse n'est pas un objet JSON")
    return contenu
