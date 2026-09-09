"""Enregistrement des classements : immuabilité, comparaison, déterminisme."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from pea import db as db_module
from pea.config import load_config
from pea.screen import asof as A
from pea.screen import runs as R
from pea.screen.score import run_screen

AS_OF = dt.date(2026, 3, 15)
ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg(tmp_path):
    source = (ROOT / "config.toml").read_text()
    (tmp_path / "config.toml").write_text(
        source.replace('data_dir = "data"', f'data_dir = "{tmp_path / "data"}"')
    )
    return load_config(tmp_path / "config.toml")


@pytest.fixture
def base(con):
    """Douze valeurs cohérentes au 15 mars 2026, dont une illiquide et une non éligible."""
    for i in range(12):
        isin = ("GB" if i == 11 else "FR") + f"{i:010d}"
        ticker = f"T{i}.PA"
        db_module.insert_df(con, "universe", pd.DataFrame([{
            "isin": isin, "name": f"Valeur {i}", "mic": "XPAR", "mnemonic": f"T{i}",
            "quote_currency": "EUR", "isin_country": isin[:2], "yf_ticker": ticker,
            "resolution": "mnemonic", "consecutive_not_found": 0,
            "first_seen": dt.date(2026, 1, 5), "last_seen": dt.date(2026, 3, 15),
            "fetched_at_utc": dt.datetime(2026, 1, 5),
        }]))
        db_module.insert_df(con, "descriptors", pd.DataFrame([{
            "ticker": ticker, "fetched_at_utc": dt.datetime(2026, 1, 10),
            "long_name": f"Valeur {i} SA", "sector": "Technology", "industry": "Software",
            "country": "United Kingdom" if i == 11 else "France", "quote_type": "EQUITY",
            "quote_currency": "EUR", "financial_currency": "EUR",
        }]))
        volume = 100 if i == 10 else 100_000   # la dixième est illiquide
        jour = dt.date(2025, 1, 1)
        lignes = []
        while jour <= AS_OF:
            if jour.weekday() < 5:
                lignes.append({
                    "ticker": ticker, "date": jour, "open": 100.0 + i, "high": 100.0 + i,
                    "low": 100.0 + i,
                    "close": 100.0 + i + (jour - dt.date(2025, 1, 1)).days * 0.01 * (i + 1),
                    "adj_close": 100.0 + i, "volume": volume, "dividend": 0.0,
                    "split_ratio": 0.0, "fetched_at_utc": dt.datetime(2026, 3, 14),
                })
            jour += dt.timedelta(days=1)
        db_module.insert_df(con, "prices", pd.DataFrame(lignes))

        for rang, annee in enumerate((2024, 2021)):
            champs = {
                "income": {"TotalRevenue": 1000.0 * (1 + 0.1 * i) ** (2 - rang),
                           "OperatingIncome": 100.0 * (1 + 0.1 * i), "EBITDA": 150.0,
                           "InterestExpense": 10.0, "NetIncome": 60.0,
                           "NetIncomeCommonStockholders": 60.0},
                "balance": {"TotalAssets": 900.0, "CurrentLiabilities": 200.0, "TotalDebt": 300.0,
                            "CashAndCashEquivalents": 100.0, "OrdinarySharesNumber": 1_000_000.0},
                "cashflow": {"FreeCashFlow": 80.0},
            }
            for statement, valeurs in champs.items():
                for field, value in valeurs.items():
                    db_module.insert_df(con, "statements", pd.DataFrame([{
                        "ticker": ticker, "statement": statement, "period_type": "annual",
                        "period_end": dt.date(annee, 12, 31), "field": field, "value": value,
                        "currency": "EUR", "fetched_at_utc": dt.datetime(2026, 1, 10),
                    }]))
        db_module.insert_df(con, "consensus", pd.DataFrame([{
            "ticker": ticker, "fetched_at_utc": dt.datetime(2026, 3, 10),
            "eps_0y_current": 6.0 + i * 0.1, "eps_0y_90d": 6.0, "n_analysts_0y": 8,
            "up_30d_0y": i % 5, "down_30d_0y": 1,
        }]))
        con.execute(
            "INSERT INTO fetch_log (ticker, endpoint, fetched_at_utc, status)"
            " VALUES (?, 'statements', ?, 'ok')",
            [ticker, dt.datetime(2026, 1, 10)],
        )
    return con


def test_classement_calcule_et_enregistre(base, cfg):
    pit = A.load_pit(base, AS_OF)
    resultat = run_screen(pit, overrides_path=cfg.overrides_path)
    assert resultat.mode == "live"
    assert resultat.n_universe == 12
    assert resultat.n_scored >= 9
    # La valeur britannique et l'illiquide sont écartées, avec la raison qui l'explique.
    raisons = dict(
        zip(resultat.scores["isin"], resultat.scores["elimination_reasons"], strict=True)
    )
    assert "non_eligible_pea" in raisons["GB0000000011"]
    assert "illiquide" in raisons["FR0000000010"]

    run_id = R.persist_run(base, resultat, cfg)
    infos, scores, couverture = R.load_run(base, run_id)
    assert infos["mode"] == "live"
    assert infos["n_scored"] == resultat.n_scored
    assert infos["max_info_date_used"] <= AS_OF
    assert len(scores) == 12
    assert not couverture.empty
    assert scores["rank"].dropna().is_unique


def test_un_classement_precedent_n_est_jamais_reecrit(base, cfg):
    pit = A.load_pit(base, AS_OF)
    premier = R.persist_run(base, run_screen(pit, overrides_path=cfg.overrides_path), cfg)
    scores_avant = R.load_run(base, premier)[1]
    second = R.persist_run(base, run_screen(pit, overrides_path=cfg.overrides_path), cfg)
    assert premier != second
    scores_apres = R.load_run(base, premier)[1]
    pd.testing.assert_frame_equal(scores_avant, scores_apres)
    assert base.execute("SELECT count(*) FROM runs").fetchone()[0] == 2


def test_deux_executions_donnent_le_meme_classement(base, cfg):
    """Le déterminisme est vérifiable : la comparaison automatique doit être vide."""
    pit = A.load_pit(base, AS_OF)
    a = run_screen(pit, overrides_path=cfg.overrides_path)
    b = run_screen(A.load_pit(base, AS_OF), overrides_path=cfg.overrides_path)
    pd.testing.assert_frame_equal(a.scores, b.scores)

    R.persist_run(base, a, cfg)
    second = R.persist_run(base, b, cfg)
    diff = R.compare_with_previous(base, second, AS_OF)
    assert diff is not None and diff.identical
    assert diff.n_common == 12


def test_comparaison_detecte_un_changement(base, cfg):
    pit = A.load_pit(base, AS_OF)
    ancien = R.persist_run(base, run_screen(pit, overrides_path=cfg.overrides_path), cfg)
    base.execute("UPDATE scores SET total_score = 1.0 WHERE run_id = ? AND rank = 1", [ancien])
    second = R.persist_run(base, run_screen(A.load_pit(base, AS_OF), overrides_path=cfg.overrides_path), cfg)
    diff = R.compare_with_previous(base, second, AS_OF)
    assert not diff.identical and diff.n_score_changed >= 1


def test_regime_reconstitue_signale(base, cfg):
    pit = A.load_pit(base, dt.date(2025, 12, 1))   # avant le démarrage du système
    resultat = run_screen(pit, overrides_path=cfg.overrides_path)
    assert resultat.mode == "reconstructed"
    assert resultat.survivorship_complete is False
    assert "classement_reconstitue" in resultat.flags
    run_id = R.persist_run(base, resultat, cfg)
    infos = R.load_run(base, run_id)[0]
    assert infos["survivorship_complete"] is False


def test_identifiant_de_run_et_version_du_code():
    identifiant = R.make_run_id(AS_OF, dt.datetime(2026, 3, 15, 22, 30, 15))
    assert identifiant == "2026-03-15_20260315T223015"
    import json
    versions = json.loads(R.code_versions())
    assert {"python", "pandas", "duckdb", "yfinance"} <= set(versions)


def test_rapports_ecrits(base, cfg):
    from pea.screen.report import write_reports

    run_id = R.persist_run(base, run_screen(A.load_pit(base, AS_OF), overrides_path=cfg.overrides_path), cfg)
    chemins = write_reports(base, run_id, cfg)
    noms = {c.name for c in chemins}
    assert noms == {"ranking.csv", "excluded.csv", "coverage.csv", "ranking.html", "data.json"}
    for chemin in chemins:
        assert chemin.is_file() and chemin.stat().st_size > 0
    html = (cfg.reports_dir / "latest" / "ranking.html").read_text(encoding="utf-8")
    assert "Classement PEA" in html
    assert "non_eligible_pea" in html          # les raisons d'exclusion sont visibles
    assert str(AS_OF) in html
    assert (cfg.reports_dir / "index.html").is_file()


def test_export_pour_l_interface(base, cfg):
    """Le fichier data.json est un contrat : noms de champs fixes, absence codée en null."""
    import json

    from pea.screen.report import write_reports

    run_id = R.persist_run(base, run_screen(A.load_pit(base, AS_OF), overrides_path=cfg.overrides_path), cfg)
    write_reports(base, run_id, cfg)
    payload = json.loads((cfg.reports_dir / "latest" / "data.json").read_text(encoding="utf-8"))

    assert payload["as_of"] == AS_OF.isoformat()
    assert payload["regime"] == "live"
    assert payload["univers"]["examinees"] == 12
    assert payload["derniere_information_utilisee"] <= AS_OF.isoformat()

    valeur = payload["valeurs"][0]
    assert valeur["rang"] == 1
    assert set(valeur["blocs"]) == {
        "croissance_qualite", "momentum", "valorisation", "bilan", "consensus"
    }
    assert "ve_sur_resultat_op" in valeur["metriques"]
    assert valeur["metriques"]["ve_sur_resultat_op"]["classe_dans_le_secteur"] is True
    assert valeur["metriques"]["marge_operationnelle"]["unite"] == "pourcent"

    # Une donnée absente vaut null, jamais zéro : c'est la règle la plus importante.
    manquantes = [
        m for m in valeur["metriques"].values() if m["valeur"] is None
    ]
    assert all(m["valeur"] is None for m in manquantes)
    assert isinstance(valeur["champs_manquants"], list)
    assert valeur["dossier"] is None            # les dossiers arrivent au lot 2

    # Les valeurs écartées portent leurs raisons.
    assert payload["ecartees"] and payload["ecartees"][0]["raisons"]
    assert payload["raisons_exclusion"][0]["valeurs"] >= 1
    # La couverture est triée du champ le moins renseigné au plus renseigné.
    presents = [c["present"] for c in payload["couverture_par_champ"]]
    assert presents == sorted(presents)


def test_rapport_reconstitue_porte_l_avertissement(base, cfg):
    from pea.screen.report import write_reports

    resultat = run_screen(A.load_pit(base, dt.date(2025, 12, 1)), overrides_path=cfg.overrides_path)
    run_id = R.persist_run(base, resultat, cfg)
    write_reports(base, run_id, cfg)
    html = (cfg.reports_dir / "latest" / "ranking.html").read_text(encoding="utf-8")
    assert "Classement reconstitué" in html
