"""Choix des valeurs qui reçoivent un dossier cette semaine.

Trois motifs, comme demandé : l'entrée dans le premier tiers du classement, un bond de plus
de quinze rangs, et toute ligne détenue dans le portefeuille fictif.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

TOP_N = 60
BOND_MINIMAL = 15
PLAFOND = 25          # environ quinze à vingt-cinq dossiers par semaine


@dataclass(frozen=True)
class Candidate:
    isin: str
    ticker: str | None
    name: str | None
    rank: int
    total_score: float
    motif: str        # top60 | bond | detenue
    rang_precedent: int | None = None


def selectionner(con, run_id: str, *, plafond: int = PLAFOND) -> list[Candidate]:
    """Sélectionne les valeurs à documenter à partir du classement `run_id`."""
    as_of = con.execute("SELECT as_of FROM runs WHERE run_id = ?", [run_id]).fetchone()
    if as_of is None:
        raise KeyError(f"classement inconnu : {run_id}")
    precedent = _classement_precedent(con, run_id, as_of[0])

    lignes = con.execute(
        """
        SELECT isin, ticker, name, rank, total_score
        FROM scores WHERE run_id = ? AND eliminated = FALSE AND rank IS NOT NULL
        ORDER BY rank
        """,
        [run_id],
    ).fetchall()

    detenues = _lignes_detenues(con)
    rangs_precedents = dict(precedent)
    candidats: dict[str, Candidate] = {}

    for isin, ticker, name, rang, score in lignes:
        ancien = rangs_precedents.get(isin)
        motif = None
        if isin in detenues:
            motif = "detenue"
        elif rang <= TOP_N:
            motif = "top60"
        elif ancien is not None and ancien - rang >= BOND_MINIMAL:
            motif = "bond"
        if motif:
            candidats[isin] = Candidate(isin, ticker, name, int(rang), float(score), motif, ancien)

    # Les lignes détenues passent toujours, puis les meilleures du classement.
    ordre = {"detenue": 0, "top60": 1, "bond": 2}
    retenus = sorted(candidats.values(), key=lambda c: (ordre[c.motif], c.rank))
    return retenus[:plafond]


def _classement_precedent(con, run_id: str, as_of: dt.date) -> list[tuple[str, int]]:
    """Le classement le plus récent antérieur à celui-ci, pour mesurer les bonds."""
    ligne = con.execute(
        """
        SELECT run_id FROM runs
        WHERE status = 'done' AND as_of < ? AND run_id <> ?
        ORDER BY as_of DESC, started_at_utc DESC LIMIT 1
        """,
        [as_of, run_id],
    ).fetchone()
    if ligne is None:
        return []
    return con.execute(
        "SELECT isin, rank FROM scores WHERE run_id = ? AND rank IS NOT NULL", [ligne[0]]
    ).fetchall()


def _lignes_detenues(con) -> set[str]:
    """Positions du portefeuille fictif. Vide tant que le lot 3 n'existe pas."""
    tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    if "positions" not in tables:
        return set()
    return {
        row[0]
        for row in con.execute("SELECT DISTINCT isin FROM positions WHERE quantite > 0").fetchall()
    }
