"""Ingestion : orchestration, reprise, journal des récupérations."""

from __future__ import annotations

import pandas as pd

from pea import db as db_module
from pea.data.provider import DESCRIPTOR_KEYS


def store_descriptors(con, ticker: str, descriptors: dict) -> bool:
    """Ajoute un instantané de métadonnées, seulement si un champ a changé.

    Renvoie True si une ligne a été écrite.
    """
    values = {key: descriptors.get(key) for key in DESCRIPTOR_KEYS}
    previous = con.execute(
        f"""
        SELECT {", ".join(DESCRIPTOR_KEYS)} FROM descriptors
        WHERE ticker = ? ORDER BY fetched_at_utc DESC LIMIT 1
        """,
        [ticker],
    ).fetchone()
    if previous is not None and list(previous) == [values[key] for key in DESCRIPTOR_KEYS]:
        return False
    row = {"ticker": ticker, "fetched_at_utc": db_module.now_utc(), **values}
    db_module.insert_df(con, "descriptors", pd.DataFrame([row]), on_conflict="replace")
    return True
