"""Enregistrement immuable des classements : un run n'est jamais réécrit."""

from __future__ import annotations

import datetime as dt
import json
import math
import subprocess
from dataclasses import dataclass

import pandas as pd

from pea import db as db_module
from pea.screen.metrics import METRIC_NAMES

SCORE_COLUMNS = (
    "run_id", "as_of", "isin", "ticker", "name", "mic", "sector", "industry", "country",
    "eliminated", "elimination_reasons", "rank", "decile", "total_score",
    "block_growth_quality", "block_momentum", "block_valuation", "block_balance",
    "block_consensus", "consensus_incomplete",
    *[f"m_{m}" for m in METRIC_NAMES],
    *[f"s_{m}" for m in METRIC_NAMES],
    "price", "price_date", "market_cap_eur", "ev_eur", "traded_value_3m_eur",
    "fy0_period_end", "statement_age_days", "statement_currency", "fx_rate_used",
    "n_required", "n_missing", "missing_fields", "missing_metrics", "coverage_ratio", "flags",
)


@dataclass(frozen=True)
class Diff:
    n_common: int
    n_added: int
    n_removed: int
    n_score_changed: int
    n_rank_changed: int

    @property
    def identical(self) -> bool:
        return self.n_added == self.n_removed == self.n_score_changed == self.n_rank_changed == 0


def git_sha() -> str | None:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if not sha:
            return None
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except (OSError, subprocess.SubprocessError):
        return None


def code_versions() -> str:
    import sys

    import duckdb
    import yfinance

    return json.dumps({
        "python": sys.version.split()[0],
        "pandas": pd.__version__,
        "duckdb": duckdb.__version__,
        "yfinance": yfinance.__version__,
    })


def make_run_id(as_of: dt.date, started: dt.datetime) -> str:
    return f"{as_of:%Y-%m-%d}_{started:%Y%m%dT%H%M%S}"


def _unique_run_id(con, as_of: dt.date, started: dt.datetime) -> str:
    """Deux classements calculés dans la même seconde restent distincts."""
    base = make_run_id(as_of, started)
    run_id, suffixe = base, 1
    while con.execute("SELECT 1 FROM runs WHERE run_id = ?", [run_id]).fetchone():
        suffixe += 1
        run_id = f"{base}-{suffixe}"
    return run_id


def persist_run(con, result, cfg, *, started_at: dt.datetime | None = None) -> str:
    """Écrit le classement et sa couverture. Les lignes déjà enregistrées ne bougent pas."""
    started_at = started_at or db_module.now_utc()
    run_id = _unique_run_id(con, result.as_of, started_at)

    scores = result.scores.copy()
    scores["run_id"] = run_id
    scores["as_of"] = result.as_of
    for colonne in SCORE_COLUMNS:
        if colonne not in scores.columns:
            scores[colonne] = None
    # Les sentinelles infinies servent au classement, pas au stockage.
    for metric in METRIC_NAMES:
        colonne = f"m_{metric}"
        scores[colonne] = scores[colonne].map(
            lambda v: None if v is None or (isinstance(v, float) and math.isinf(v)) else v
        )
    scores = scores[list(SCORE_COLUMNS)]

    couverture = result.coverage.copy()
    couverture["run_id"] = run_id

    con.execute(
        """
        INSERT INTO runs (run_id, as_of, cutoff_utc, mode, survivorship_complete, started_at_utc,
                          finished_at_utc, status, git_sha, config_sha256, code_versions,
                          n_universe, n_scored, n_eliminated, max_fetched_at_used_utc,
                          max_info_date_used)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'done', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            run_id, result.as_of,
            dt.datetime.combine(result.as_of + dt.timedelta(days=1), dt.time()),
            result.mode, result.survivorship_complete, started_at, db_module.now_utc(),
            git_sha(), getattr(cfg, "config_sha256", None), code_versions(),
            result.n_universe, result.n_scored, result.n_eliminated,
            result.audit.max_fetched_at_used, result.audit.max_info_date_used,
        ],
    )
    db_module.insert_df(con, "scores", scores)
    db_module.insert_df(con, "coverage", couverture[["run_id", "population", "field", "n_present", "n_missing"]])
    return run_id


def previous_run(con, as_of: dt.date, *, exclude: str | None = None) -> str | None:
    row = con.execute(
        """
        SELECT run_id FROM runs
        WHERE as_of = ? AND status = 'done' AND (? IS NULL OR run_id <> ?)
        ORDER BY started_at_utc DESC LIMIT 1
        """,
        [as_of, exclude, exclude],
    ).fetchone()
    return row[0] if row else None


def compare_with_previous(con, run_id: str, as_of: dt.date) -> Diff | None:
    """Compare au dernier classement de la même date : deux exécutions doivent concorder."""
    ancien = previous_run(con, as_of, exclude=run_id)
    if ancien is None:
        return None
    row = con.execute(
        """
        WITH a AS (SELECT isin, total_score, rank FROM scores WHERE run_id = ?),
             b AS (SELECT isin, total_score, rank FROM scores WHERE run_id = ?)
        SELECT
            (SELECT count(*) FROM a JOIN b USING (isin)),
            (SELECT count(*) FROM b WHERE isin NOT IN (SELECT isin FROM a)),
            (SELECT count(*) FROM a WHERE isin NOT IN (SELECT isin FROM b)),
            (SELECT count(*) FROM a JOIN b USING (isin)
             WHERE a.total_score IS DISTINCT FROM b.total_score),
            (SELECT count(*) FROM a JOIN b USING (isin) WHERE a.rank IS DISTINCT FROM b.rank)
        """,
        [ancien, run_id],
    ).fetchone()
    return Diff(*row)


def load_run(con, run_id: str) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    infos = con.execute("SELECT * FROM runs WHERE run_id = ?", [run_id]).df()
    if infos.empty:
        raise KeyError(f"classement inconnu : {run_id}")
    scores = con.execute(
        "SELECT * FROM scores WHERE run_id = ? ORDER BY rank NULLS LAST, isin", [run_id]
    ).df()
    couverture = con.execute(
        "SELECT * FROM coverage WHERE run_id = ? ORDER BY population, field", [run_id]
    ).df()
    ligne = infos.iloc[0].to_dict()
    for champ in ("as_of", "max_info_date_used"):
        valeur = ligne.get(champ)
        ligne[champ] = None if valeur is None or pd.isna(valeur) else pd.Timestamp(valeur).date()
    return ligne, scores, couverture


def latest_run_id(con, as_of: dt.date | None = None) -> str | None:
    if as_of is not None:
        return previous_run(con, as_of)
    row = con.execute(
        "SELECT run_id FROM runs WHERE status = 'done' ORDER BY as_of DESC, started_at_utc DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None
