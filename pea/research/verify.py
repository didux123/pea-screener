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
# Un chiffre calculé à partir de faits cités tolère un écart plus large : « multiplié par
# 2,5 » pour un rapport de 2,57 reste une lecture honnête.
TOLERANCE_CALCUL = 0.08
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
    # Une lettre peut coller au chiffre : la presse écrit « EUR750 million ». Seuls un autre
    # chiffre ou un séparateur décimal interdisent la reconnaissance, pour ne pas couper un
    # nombre en deux. Les références du type [F19] sont retirées du texte au préalable.
    r"(?<![\d.,])"
    r"([+-]?(?:\d{1,3}(?:[   ]\d{3})+|\d+(?:[.,]\d+)?))"
    r"\s*(%|milliards?|millions?|milliers?|mds?|m€)?",
    re.IGNORECASE,
)

# « moyenne mobile à 200 jours », « performance sur 12 mois » : ces nombres nomment une
# fenêtre d'observation, ils n'affirment rien sur la société.
DUREE = re.compile(
    r"^\s*(?:jours?|mois|ans?|années?|semestres?|trimestres?|semaines?)\b", re.IGNORECASE
)

# Références citées dans une phrase : « [F41, F28] ».
REFS_CITEES = re.compile(r"\[([FSN]\d{1,3}(?:\s*,\s*[FSN]\d{1,3})*)\]")


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
            for nombre in nombres_du_texte(f"{fait.libelle} {fait.texte or ''}"):
                valeurs.add(nombre.valeur)
    return valeurs


@dataclass(frozen=True)
class NombreEcrit:
    """Un chiffre tel qu'il figure dans le texte, avec la précision qu'il annonce.

    « 10 milliards » vaut dix milliards mais n'affirme qu'un ordre de grandeur au milliard
    près ; « 0,7 % » affirme le dixième de point. La comparaison doit se faire à cette
    échelle, sans quoi tout arrondi honnête passerait pour une invention.
    """

    brut: str
    valeur: float
    echelle: float      # unité dans laquelle le chiffre a été écrit
    decimales: int      # décimales effectivement écrites

    def arrondi(self, valeur: float) -> float:
        return round(valeur / self.echelle, self.decimales)


def nombres_du_texte(texte: str) -> list[NombreEcrit]:
    """Extrait les nombres d'un texte français, avec leur échelle et leur précision.

    Les durées sont ignorées : « moyenne mobile à 200 jours » nomme une fenêtre de calcul,
    ce n'est pas une affirmation chiffrée sur la société.
    """
    # Les références sont retirées d'abord : « [F19] » ne doit pas se lire comme le nombre 19.
    texte = REFS_CITEES.sub(" ", texte or "")
    trouves: list[NombreEcrit] = []
    for correspondance in NOMBRE.finditer(texte):
        if DUREE.match(texte[correspondance.end():]):
            continue
        brut, suffixe = correspondance.group(1), (correspondance.group(2) or "").lower()
        nettoye = brut.replace(" ", "").replace("\u00a0", "").replace("\u202f", "")
        if nettoye.count(",") == 1 and "." not in nettoye:
            nettoye = nettoye.replace(",", ".")
        else:
            nettoye = nettoye.replace(",", "")
        try:
            valeur = float(nettoye)
        except ValueError:
            continue
        # La précision se lit sur les chiffres seuls, jamais sur l'unité qui les suit.
        fraction = nettoye.split(".")
        decimales = len(fraction[1]) if len(fraction) > 1 else 0
        echelle = MULTIPLICATEURS.get(suffixe, 1.0)
        trouves.append(
            NombreEcrit(correspondance.group(0).strip(), valeur * echelle, echelle, decimales)
        )
    return trouves


def _valeurs_derivees(texte: str, par_ref: dict) -> set[float]:
    """Ce qu'on peut calculer à partir des faits cités dans la phrase elle-même.

    Un analyste écrit « les charges d'intérêt ont été multipliées par 2,5 [F41, F28] ». Le
    2,5 n'est dans aucun fait, mais il se déduit des deux faits cités. Exiger que tout
    chiffre figure tel quel dans les données interdirait tout raisonnement. On n'accepte le
    calcul que si le modèle a cité les opérandes : la vérification reste possible.
    """
    valeurs: list[float] = []
    for groupe in REFS_CITEES.findall(texte):
        for ref in (r.strip() for r in groupe.split(",")):
            fait = par_ref.get(ref)
            if fait is not None and fait.valeur is not None:
                valeurs.append(float(fait.valeur))

    derivees: set[float] = set()
    for a in valeurs:
        for b in valeurs:
            if a is b:
                continue
            derivees.update({a - b, a + b})
            if b != 0:
                rapport_ab = a / b
                derivees.add(rapport_ab)
                derivees.add(rapport_ab * 100)          # écrit en pourcentage
                derivees.add((a - b) / abs(b))          # variation relative
                derivees.add((a - b) / abs(b) * 100)
    return derivees


def _est_source(nombre: NombreEcrit, autorisees: set[float], tolerance: float = TOLERANCE) -> bool:
    """Le chiffre écrit doit être un arrondi plausible d'un fait fourni.

    Trois assouplissements, tous constatés sur de vrais dossiers. Le signe n'est pas
    discriminant, car en français le sens est porté par les mots : « une baisse de 3,1 % »
    et « -3,1 % » désignent le même fait. La comparaison se fait à l'échelle d'écriture, si
    bien qu'un chiffre d'affaires de 10,2 milliards écrit « 10 milliards » est correctement
    rapporté. Et un écart inférieur à la tolérance relative passe aussi, pour les nombres
    dont l'arrondi ne se lit pas dans l'écriture.
    """
    cible = abs(nombre.valeur)
    if cible in PETITS_ENTIERS and float(cible).is_integer():
        return True
    if float(cible).is_integer() and int(cible) in ANNEES:
        return True

    for reference in autorisees:
        attendu = abs(reference)
        if attendu == 0:
            if cible < 1e-9:
                return True
            continue
        if abs(cible - attendu) <= attendu * tolerance:
            return True
        if nombre.arrondi(attendu) == nombre.arrondi(cible):
            return True
    return False


def _textes_narratifs(dossier: Dossier) -> list[tuple[str, str]]:
    """Les champs rédigés, où un chiffre inventé pourrait se glisser.

    La méthode de valorisation en est absente, au même titre que le seuil d'invalidation :
    elle expose les multiples et les décotes que l'analyste retient, qui sont des choix et
    non des faits. Les bornes de la fourchette, elles, restent contrôlées.
    """
    textes = [
        ("activite", dossier.activite),
        ("origine_du_chiffre_affaires", dossier.origine_du_chiffre_affaires),
        ("avantage_concurrentiel", dossier.avantage_concurrentiel),
        ("ce_qui_le_ferait_disparaitre", dossier.ce_qui_le_ferait_disparaitre),
        ("hausse_justifiee", dossier.hausse_justifiee.explication),
        ("debat.haussier", dossier.debat.haussier),
        ("debat.baissier", dossier.debat.baissier),
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
    par_ref = travail.par_ref()
    for champ, texte in _textes_narratifs(dossier):
        if not texte or NON_DISPONIBLE in texte.lower():
            continue
        derivees = _valeurs_derivees(texte, par_ref)
        for nombre in nombres_du_texte(texte):
            # Un ratio calculé est approximatif par nature : « multiplié par 2,5 » pour 2,57
            # reste une lecture honnête, dès lors que les opérandes sont cités.
            if _est_source(nombre, autorisees) or _est_source(nombre, derivees, TOLERANCE_CALCUL):
                continue
            rapport.nombres_non_sources.append(f"{champ} : « {nombre.brut} »")

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
