"""Vérification d'un dossier : aucun chiffre inventé, aucune recommandation d'ordre.

Un dossier qui échoue ici n'est pas enregistré comme valide. C'est le garde-fou qui
distingue une thèse argumentée d'un texte plausible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pea.research.context import DossierDeTravail
from pea.research.schema import NON_DISPONIBLE, Dossier

# Tolérance relative sur un chiffre cité : le modèle arrondit, c'est légitime.
TOLERANCE = 0.02
# Les petits entiers servent à compter (trois risques, deux critères) : ils ne prétendent rien.
PETITS_ENTIERS = set(range(0, 13))
# Une année n'est pas une affirmation chiffrée : les sociétés citent souvent leur fondation.
ANNEES = set(range(1700, 2101))

MULTIPLICATEURS = {
    "milliard": 1e9, "milliards": 1e9, "md": 1e9, "mds": 1e9,
    "million": 1e6, "millions": 1e6, "m€": 1e6,
    "millier": 1e3, "milliers": 1e3,
}

# Un dossier ne recommande pas d'ordre : ces tournures sont interdites.
TOURNURES_INTERDITES = (
    (r"\b(?:il faut|je recommande|nous recommandons|on doit)\s+(?:d'|d’)?(?:acheter|vendre)",
     "recommandation d'ordre"),
    (r"\b(?:acheter|vendre)\s+(?:à|au|autour de)\s+\d", "prix d'achat ou de vente"),
    (r"\b\d{1,3}\s*%\s+(?:du|de la)\s+portefeuille", "taille de position"),
    (r"\bpond[ée]ration\s+(?:de|à)\s+\d", "pondération"),
    (r"\bposition\s+de\s+\d{1,3}\s*%", "taille de position"),
    (r"\bpasser\s+un\s+ordre\b", "ordre"),
)

NOMBRE = re.compile(
    r"(?<![\w.])"
    r"([+-]?(?:\d{1,3}(?:[   ]\d{3})+|\d+(?:[.,]\d+)?))"
    r"\s*(%|milliards?|millions?|milliers?|mds?|m€)?",
    re.IGNORECASE,
)


@dataclass
class Rapport:
    """Résultat de la vérification. `valide` conditionne l'enregistrement du dossier."""

    valide: bool = True
    refs_inconnues: list[str] = field(default_factory=list)
    nombres_non_sources: list[str] = field(default_factory=list)
    tournures_interdites: list[str] = field(default_factory=list)
    textes_suspects: list[str] = field(default_factory=list)

    def motifs(self) -> list[str]:
        motifs = []
        if self.refs_inconnues:
            motifs.append(f"références inconnues : {', '.join(sorted(set(self.refs_inconnues))[:6])}")
        if self.nombres_non_sources:
            motifs.append(
                f"chiffres sans source : {', '.join(sorted(set(self.nombres_non_sources))[:8])}"
            )
        if self.tournures_interdites:
            motifs.append(f"tournures interdites : {', '.join(sorted(set(self.tournures_interdites)))}")
        return motifs


def _valeurs_autorisees(travail: DossierDeTravail) -> set[float]:
    """Toutes les valeurs qu'un dossier a le droit de citer."""
    valeurs: set[float] = set()
    for fait in travail.faits:
        if fait.valeur is None:
            continue
        valeurs.add(float(fait.valeur))
        if fait.unite in ("pourcent", "points"):
            valeurs.add(float(fait.valeur) * 100)      # écrit en pourcentage
        if fait.unite == "eur":
            valeurs.add(float(fait.valeur) / 1e6)      # écrit en millions
            valeurs.add(float(fait.valeur) / 1e9)      # écrit en milliards
    # Les chiffres présents dans les articles cités sont eux aussi sourcés.
    for fait in travail.faits:
        if fait.origine == "presse":
            for _, valeur in nombres_du_texte(f"{fait.libelle} {fait.texte or ''}"):
                valeurs.add(valeur)
    return valeurs


def nombres_du_texte(texte: str) -> list[tuple[str, float]]:
    """Extrait les nombres d'un texte français, avec leur multiplicateur éventuel."""
    trouves: list[tuple[str, float]] = []
    for correspondance in NOMBRE.finditer(texte or ""):
        brut, suffixe = correspondance.group(1), (correspondance.group(2) or "").lower()
        nettoye = brut.replace(" ", "").replace(" ", "").replace(" ", "")
        if nettoye.count(",") == 1 and "." not in nettoye:
            nettoye = nettoye.replace(",", ".")
        else:
            nettoye = nettoye.replace(",", "")
        try:
            valeur = float(nettoye)
        except ValueError:
            continue
        if suffixe in MULTIPLICATEURS:
            valeur *= MULTIPLICATEURS[suffixe]
        trouves.append((correspondance.group(0).strip(), valeur))
    return trouves


def _decimales(brut: str) -> int:
    """Précision à laquelle le chiffre a été écrit : « 0,5 » en annonce une, « 42,68 » deux."""
    partie = re.split(r"[.,]", brut.split()[0].replace(" ", "").replace(" ", ""))
    return len(partie[-1]) if len(partie) > 1 else 0


def _est_source(valeur: float, autorisees: set[float], brut: str = "") -> bool:
    """Le chiffre écrit doit être un arrondi plausible d'un fait fourni.

    Deux assouplissements, tirés des premiers dossiers réels. Le signe n'est pas
    discriminant : en français le sens est porté par les mots, « une baisse de 3,1 % » et
    « -3,1 % » désignent le même fait. Et la comparaison se fait à la précision d'écriture :
    un fait à -0,53 % écrit « 0,5 % » est correctement rapporté, pas inventé.
    """
    cible = abs(valeur)
    if cible in PETITS_ENTIERS and float(cible).is_integer():
        return True
    if float(cible).is_integer() and int(cible) in ANNEES:
        return True

    precision = _decimales(brut) if brut else None
    for reference in autorisees:
        attendu = abs(reference)
        if attendu == 0:
            if cible < 1e-9:
                return True
            continue
        if abs(cible - attendu) <= attendu * TOLERANCE:
            return True
        if precision is not None and round(attendu, precision) == round(cible, precision):
            return True
    return False


def _textes_narratifs(dossier: Dossier) -> list[tuple[str, str]]:
    """Les champs rédigés, où un chiffre inventé pourrait se glisser."""
    textes = [
        ("activite", dossier.activite),
        ("origine_du_chiffre_affaires", dossier.origine_du_chiffre_affaires),
        ("avantage_concurrentiel", dossier.avantage_concurrentiel),
        ("ce_qui_le_ferait_disparaitre", dossier.ce_qui_le_ferait_disparaitre),
        ("hausse_justifiee", dossier.hausse_justifiee.explication),
        ("debat.haussier", dossier.debat.haussier),
        ("debat.baissier", dossier.debat.baissier),
        ("fourchette.methode", dossier.fourchette_valorisation.methode),
    ]
    textes += [(f"moteur[{i}]", t) for i, t in enumerate(dossier.moteurs_de_croissance)]
    textes += [(f"signe[{i}]", t) for i, t in enumerate(dossier.signes_alerte)]
    textes += [(f"risque[{i}]", r.explication) for i, r in enumerate(dossier.risques)]
    textes += [(f"catalyseur[{i}]", c.evenement) for i, c in enumerate(dossier.catalyseurs)]
    for i, critere in enumerate(dossier.criteres_invalidation):
        # Le seuil est choisi par l'analyste, pas observé : c'est le niveau à partir duquel il
        # déclare sa thèse fausse. Il n'a donc pas à figurer dans les faits fournis.
        textes.append((f"invalidation[{i}]", f"{critere.critere} {critere.mesure}"))
    return textes


def verifier(dossier: Dossier, travail: DossierDeTravail, *, suspects: list[str] | None = None) -> Rapport:
    """Contrôle le dossier produit contre les faits qui lui ont été fournis."""
    rapport = Rapport(textes_suspects=list(suspects or []))
    connus = set(travail.par_ref())

    for ref in dossier.refs_citees() | {s.ref for s in dossier.sources}:
        if ref not in connus:
            rapport.refs_inconnues.append(ref)

    autorisees = _valeurs_autorisees(travail)
    for champ, texte in _textes_narratifs(dossier):
        if not texte or NON_DISPONIBLE in texte.lower():
            continue
        for brut, valeur in nombres_du_texte(texte):
            if not _est_source(valeur, autorisees, brut):
                rapport.nombres_non_sources.append(f"{champ} : « {brut} »")

    # La fourchette de valorisation est un jugement, mais ses bornes doivent rester
    # dans un ordre de grandeur cohérent avec le cours fourni.
    fourchette = dossier.fourchette_valorisation
    cours = next((f.valeur for f in travail.faits if f.libelle == "cours"), None)
    if cours and fourchette.basse and fourchette.haute:
        if not 0.1 * cours <= fourchette.basse <= 10 * cours:
            rapport.nombres_non_sources.append(f"fourchette.basse : {fourchette.basse}")
        if fourchette.haute < fourchette.basse:
            rapport.nombres_non_sources.append("fourchette : borne haute sous la borne basse")

    tout = " ".join(texte for _, texte in _textes_narratifs(dossier))
    for motif, libelle in TOURNURES_INTERDITES:
        if re.search(motif, tout, re.IGNORECASE):
            rapport.tournures_interdites.append(libelle)

    rapport.valide = not (
        rapport.refs_inconnues or rapport.nombres_non_sources or rapport.tournures_interdites
    )
    return rapport
