from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from pea import db as db_module


def test_ddl_cree_toutes_les_tables(con):
    present = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    assert set(db_module.TABLES) <= present
    assert con.execute("SELECT max(version) FROM schema_version").fetchone()[0] == db_module.SCHEMA_VERSION


def test_connexion_idempotente(tmp_path):
    path = tmp_path / "x.duckdb"
    db_module.connect(path).close()
    con = db_module.connect(path)
    assert con.execute("SELECT count(*) FROM schema_version").fetchone()[0] == 1
    con.close()


def test_valeurs_manquantes_restent_null(con):
    df = pd.DataFrame(
        {
            "ticker": ["AI.PA", "XX.PA"],
            "date": [dt.date(2026, 9, 8), dt.date(2026, 9, 8)],
            "close": [180.5, pd.NA],
            "volume": [1000, None],
            "dividend": [0.0, 0.0],
            "split_ratio": [0.0, 0.0],
            "fetched_at_utc": [db_module.now_utc()] * 2,
        }
    )
    assert db_module.insert_df(con, "prices", df) == 2
    rows = con.execute("SELECT ticker, close, volume FROM prices ORDER BY ticker").fetchall()
    assert rows[0] == ("AI.PA", 180.5, 1000)
    assert rows[1] == ("XX.PA", None, None)  # jamais imputé à zéro


def test_on_conflict_ignore_et_replace(con):
    base = pd.DataFrame(
        {
            "quote_ccy": ["USD"],
            "date": [dt.date(2026, 9, 8)],
            "rate": [1.10],
            "fetched_at_utc": [db_module.now_utc()],
        }
    )
    db_module.insert_df(con, "fx_rates", base)
    modifie = base.assign(rate=[1.20])
    db_module.insert_df(con, "fx_rates", modifie, on_conflict="ignore")
    assert con.execute("SELECT rate FROM fx_rates").fetchone()[0] == 1.10
    db_module.insert_df(con, "fx_rates", modifie, on_conflict="replace")
    assert con.execute("SELECT rate FROM fx_rates").fetchone()[0] == 1.20
    assert con.execute("SELECT count(*) FROM fx_rates").fetchone()[0] == 1


def test_colonne_inconnue_rejetee(con):
    df = pd.DataFrame({"ticker": ["AI.PA"], "inconnue": [1]})
    with pytest.raises(ValueError, match="colonnes inconnues"):
        db_module.insert_df(con, "prices", df)


def test_dataframe_vide(con):
    assert db_module.insert_df(con, "prices", pd.DataFrame()) == 0


def test_now_utc_naif_et_strictement_croissant():
    """L'horodatage sert d'estampille de version : deux appels ne doivent pas se confondre."""
    premier = db_module.now_utc()
    second = db_module.now_utc()
    assert premier.tzinfo is None
    assert second > premier
