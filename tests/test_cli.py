"""Ligne de commande : codes de sortie, fichiers produits."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from pea import cli

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def projet(tmp_path):
    """Un projet minimal : configuration pointant vers un répertoire de données vide."""
    source = (ROOT / "config.toml").read_text()
    chemin = tmp_path / "config.toml"
    chemin.write_text(source.replace('data_dir = "data"', f'data_dir = "{tmp_path / "data"}"'))
    return chemin


def test_configuration_absente(capsys):
    assert cli.main(["--config", "/inexistant.toml", "status"]) == cli.EXIT_USAGE
    assert "Configuration" in capsys.readouterr().err


def test_date_mal_formee(projet):
    with pytest.raises(SystemExit) as sortie:
        cli.main(["--config", str(projet), "screen", "--as-of", "15/03/2026"])
    assert sortie.value.code == cli.EXIT_USAGE


def test_status_sur_base_vide(projet, capsys):
    assert cli.main(["--config", str(projet), "status"]) == cli.EXIT_OK
    assert "valeurs actives" in capsys.readouterr().out


def test_research_sans_cle_refuse_de_tourner(projet, capsys, monkeypatch):
    """Sans clé d'accès au modèle, la commande explique quoi faire plutôt que d'échouer."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert cli.main(["--config", str(projet), "research"]) == cli.EXIT_USAGE
    assert ".env" in capsys.readouterr().err


def test_research_sans_classement(projet, capsys, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "cle-de-test")
    assert cli.main(["--config", str(projet), "research"]) == cli.EXIT_USAGE
    assert "make screen" in capsys.readouterr().err


def test_screen_sans_univers(projet, capsys):
    assert cli.main(["--config", str(projet), "screen"]) == cli.EXIT_USAGE
    assert "make universe" in capsys.readouterr().err


def test_report_sans_classement(projet, capsys):
    assert cli.main(["--config", str(projet), "report"]) == cli.EXIT_USAGE
    assert "Aucun classement" in capsys.readouterr().err


def test_base_verrouillee(projet, capsys, monkeypatch):
    """DuckDB n'accepte qu'un seul processus écrivain : le cron nocturne et une commande
    lancée à la main ne doivent pas se marcher dessus."""
    import duckdb

    from pea import db as db_module

    def occupee(*args, **kwargs):
        raise duckdb.IOException("Could not set lock on file")

    monkeypatch.setattr(db_module, "connect", occupee)
    assert cli.main(["--config", str(projet), "status"]) == cli.EXIT_DB_LOCKED
    assert "verrouillée" in capsys.readouterr().err


def test_screen_bout_en_bout(projet, capsys, monkeypatch):
    """Un classement complet : base peuplée, classement calculé, fichiers écrits."""
    import pandas as pd

    from pea import db as db_module
    from pea.config import load_config

    cfg = load_config(projet)
    con = db_module.connect(cfg.db_path)
    for i in range(12):
        ticker = f"T{i}.PA"
        db_module.insert_df(con, "universe", pd.DataFrame([{
            "isin": f"FR{i:010d}", "name": f"Valeur {i}", "mic": "XPAR", "mnemonic": f"T{i}",
            "quote_currency": "EUR", "isin_country": "FR", "yf_ticker": ticker,
            "resolution": "mnemonic", "consecutive_not_found": 0,
            "first_seen": dt.date(2026, 1, 5), "last_seen": dt.date(2026, 3, 15),
            "fetched_at_utc": dt.datetime(2026, 1, 5),
        }]))
        db_module.insert_df(con, "descriptors", pd.DataFrame([{
            "ticker": ticker, "fetched_at_utc": dt.datetime(2026, 1, 10),
            "long_name": f"Valeur {i} SA", "sector": "Technology", "country": "France",
            "quote_type": "EQUITY", "quote_currency": "EUR", "financial_currency": "EUR",
        }]))
        lignes, jour = [], dt.date(2025, 1, 1)
        while jour <= dt.date(2026, 3, 15):
            if jour.weekday() < 5:
                lignes.append({
                    "ticker": ticker, "date": jour, "close": 100.0 + i, "open": 100.0 + i,
                    "high": 100.0 + i, "low": 100.0 + i, "adj_close": 100.0 + i,
                    "volume": 100_000, "dividend": 0.0, "split_ratio": 0.0,
                    "fetched_at_utc": dt.datetime(2026, 3, 14),
                })
            jour += dt.timedelta(days=1)
        db_module.insert_df(con, "prices", pd.DataFrame(lignes))
        for annee in (2024, 2021):
            for statement, champs in (
                ("income", {"TotalRevenue": 1000.0 + i, "OperatingIncome": 100.0 + i,
                            "EBITDA": 150.0, "NetIncome": 60.0}),
                ("balance", {"TotalAssets": 900.0, "CurrentLiabilities": 200.0,
                             "TotalDebt": 300.0, "CashAndCashEquivalents": 100.0,
                             "OrdinarySharesNumber": 1_000_000.0}),
                ("cashflow", {"FreeCashFlow": 80.0}),
            ):
                for field, value in champs.items():
                    db_module.insert_df(con, "statements", pd.DataFrame([{
                        "ticker": ticker, "statement": statement, "period_type": "annual",
                        "period_end": dt.date(annee, 12, 31), "field": field, "value": value,
                        "currency": "EUR", "fetched_at_utc": dt.datetime(2026, 1, 10),
                    }]))
        con.execute(
            "INSERT INTO fetch_log (ticker, endpoint, fetched_at_utc, status)"
            " VALUES (?, 'statements', ?, 'ok')",
            [ticker, dt.datetime(2026, 1, 10)],
        )
    con.close()

    assert cli.main(["--config", str(projet), "screen", "--as-of", "2026-03-15"]) == cli.EXIT_OK
    sortie = capsys.readouterr().out
    assert "régime live" in sortie
    assert "12 valeurs examinées" in sortie

    dossier = cfg.reports_dir / "2026-03-15"
    for nom in ("ranking.csv", "excluded.csv", "coverage.csv", "ranking.html"):
        assert (dossier / nom).is_file()

    # Une seconde exécution doit donner exactement le même classement.
    assert cli.main(["--config", str(projet), "screen", "--as-of", "2026-03-15"]) == cli.EXIT_OK
    assert "Identique au classement précédent" in capsys.readouterr().out

    # Le rapport peut être regénéré sans recalculer.
    assert cli.main(["--config", str(projet), "report", "--as-of", "2026-03-15"]) == cli.EXIT_OK

    # Une date antérieure au démarrage produit un classement reconstitué, signalé comme tel.
    assert cli.main(["--config", str(projet), "screen", "--as-of", "2025-06-30"]) == cli.EXIT_OK
    assert "reconstitué" in capsys.readouterr().out
