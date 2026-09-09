"""Connexion DuckDB et schéma. Un seul fichier de base, DDL idempotent.

Toute table porte la date de récupération (fetched_at_utc) et une date d'information
explicite : c'est ce qui rend le calcul « à une date passée » vérifiable.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pandas as pd

SCHEMA_VERSION = 2

DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER NOT NULL,
        applied_at_utc TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS universe_files (
        source VARCHAR NOT NULL,
        file_date DATE NOT NULL,
        path VARCHAR NOT NULL,
        sha256 VARCHAR NOT NULL,
        n_rows INTEGER NOT NULL,
        loaded_at_utc TIMESTAMP NOT NULL,
        PRIMARY KEY (source, file_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS listings (
        isin VARCHAR NOT NULL,
        mic VARCHAR NOT NULL,
        mnemonic VARCHAR,
        name VARCHAR,
        quote_currency VARCHAR,
        source VARCHAR NOT NULL,
        first_seen DATE NOT NULL,
        last_seen DATE NOT NULL,
        delisted_at DATE,
        PRIMARY KEY (isin, mic)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS universe (
        isin VARCHAR PRIMARY KEY,
        name VARCHAR NOT NULL,
        mic VARCHAR NOT NULL,
        mnemonic VARCHAR,
        quote_currency VARCHAR,
        isin_country VARCHAR NOT NULL,
        yf_ticker VARCHAR,
        resolution VARCHAR NOT NULL,          -- mnemonic|lookup|manual|unresolved|lost
        resolved_at_utc TIMESTAMP,
        consecutive_not_found INTEGER NOT NULL DEFAULT 0,
        first_seen DATE NOT NULL,
        last_seen DATE NOT NULL,
        delisted_at DATE,
        fetched_at_utc TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS universe_membership (
        list_date DATE NOT NULL,
        isin VARCHAR NOT NULL,
        mic VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        fetched_at_utc TIMESTAMP NOT NULL,
        PRIMARY KEY (list_date, isin, mic)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS prices (
        ticker VARCHAR NOT NULL,
        date DATE NOT NULL,
        open DOUBLE,
        high DOUBLE,
        low DOUBLE,
        close DOUBLE,
        adj_close DOUBLE,                     -- conservé pour référence, jamais utilisé dans les calculs
        volume BIGINT,
        dividend DOUBLE NOT NULL DEFAULT 0,
        split_ratio DOUBLE NOT NULL DEFAULT 0,  -- convention Yahoo : 0 = pas d'événement
        fetched_at_utc TIMESTAMP NOT NULL,
        PRIMARY KEY (ticker, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fx_rates (
        quote_ccy VARCHAR NOT NULL,
        date DATE NOT NULL,
        rate DOUBLE NOT NULL,                 -- 1 EUR = rate quote_ccy
        fetched_at_utc TIMESTAMP NOT NULL,
        PRIMARY KEY (quote_ccy, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS statements (
        ticker VARCHAR NOT NULL,
        statement VARCHAR NOT NULL,           -- income|balance|cashflow
        period_type VARCHAR NOT NULL,         -- annual|quarterly
        period_end DATE NOT NULL,
        field VARCHAR NOT NULL,
        value DOUBLE NOT NULL,
        currency VARCHAR,
        fetched_at_utc TIMESTAMP NOT NULL,    -- estampille de version : nouvelle ligne si la valeur change
        PRIMARY KEY (ticker, statement, period_type, period_end, field, fetched_at_utc)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS earnings_dates (
        ticker VARCHAR NOT NULL,
        event_date DATE NOT NULL,
        eps_estimate DOUBLE,
        eps_reported DOUBLE,
        fetched_at_utc TIMESTAMP NOT NULL,
        PRIMARY KEY (ticker, event_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS descriptors (
        ticker VARCHAR NOT NULL,
        fetched_at_utc TIMESTAMP NOT NULL,
        long_name VARCHAR,
        sector VARCHAR,
        industry VARCHAR,
        country VARCHAR,
        quote_type VARCHAR,
        exchange VARCHAR,
        quote_currency VARCHAR,
        financial_currency VARCHAR,
        shares_outstanding DOUBLE,
        market_cap DOUBLE,
        avg_volume_3m DOUBLE,
        PRIMARY KEY (ticker, fetched_at_utc)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS consensus (
        ticker VARCHAR NOT NULL,
        fetched_at_utc TIMESTAMP NOT NULL,
        currency VARCHAR,
        eps_0y_current DOUBLE, eps_0y_7d DOUBLE, eps_0y_30d DOUBLE, eps_0y_60d DOUBLE, eps_0y_90d DOUBLE,
        eps_1y_current DOUBLE, eps_1y_7d DOUBLE, eps_1y_30d DOUBLE, eps_1y_60d DOUBLE, eps_1y_90d DOUBLE,
        up_30d_0y INTEGER, down_30d_0y INTEGER, up_30d_1y INTEGER, down_30d_1y INTEGER,
        n_analysts_0y INTEGER, n_analysts_1y INTEGER,
        PRIMARY KEY (ticker, fetched_at_utc)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fetch_log (
        ticker VARCHAR,
        endpoint VARCHAR NOT NULL,
        fetched_at_utc TIMESTAMP NOT NULL,
        status VARCHAR NOT NULL,   -- ok|empty|not_found|rate_limited|error|price_reload_split|price_corrected
        n_rows INTEGER,
        error VARCHAR,
        cache_path VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id VARCHAR PRIMARY KEY,
        as_of DATE NOT NULL,
        cutoff_utc TIMESTAMP NOT NULL,
        mode VARCHAR NOT NULL,                -- live|reconstructed
        survivorship_complete BOOLEAN NOT NULL,
        started_at_utc TIMESTAMP NOT NULL,
        finished_at_utc TIMESTAMP,
        status VARCHAR NOT NULL,              -- running|done|failed
        git_sha VARCHAR,
        config_sha256 VARCHAR,
        code_versions VARCHAR,
        n_universe INTEGER,
        n_scored INTEGER,
        n_eliminated INTEGER,
        max_fetched_at_used_utc TIMESTAMP,
        max_info_date_used DATE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scores (
        run_id VARCHAR NOT NULL,
        as_of DATE NOT NULL,
        isin VARCHAR NOT NULL,
        ticker VARCHAR,
        name VARCHAR,
        mic VARCHAR,
        sector VARCHAR,
        industry VARCHAR,
        country VARCHAR,
        eliminated BOOLEAN NOT NULL,
        elimination_reasons VARCHAR,
        rank INTEGER,
        decile INTEGER,
        total_score DOUBLE,
        block_growth_quality DOUBLE,
        block_momentum DOUBLE,
        block_valuation DOUBLE,
        block_balance DOUBLE,
        block_consensus DOUBLE,
        consensus_incomplete BOOLEAN,
        m_rev_cagr_3y DOUBLE, m_opinc_cagr_3y DOUBLE, m_op_margin DOUBLE, m_op_margin_trend_3y DOUBLE,
        m_roce DOUBLE, m_cash_conversion DOUBLE,
        m_mom_12_1 DOUBLE, m_mom_6_1 DOUBLE, m_dist_sma200 DOUBLE,
        m_ev_ebit DOUBLE, m_fcf_yield DOUBLE, m_pe DOUBLE,
        m_nd_ebitda DOUBLE, m_interest_cov DOUBLE, m_dilution_3y DOUBLE,
        m_eps_rev_3m DOUBLE, m_net_revisions DOUBLE,
        s_rev_cagr_3y DOUBLE, s_opinc_cagr_3y DOUBLE, s_op_margin DOUBLE, s_op_margin_trend_3y DOUBLE,
        s_roce DOUBLE, s_cash_conversion DOUBLE,
        s_mom_12_1 DOUBLE, s_mom_6_1 DOUBLE, s_dist_sma200 DOUBLE,
        s_ev_ebit DOUBLE, s_fcf_yield DOUBLE, s_pe DOUBLE,
        s_nd_ebitda DOUBLE, s_interest_cov DOUBLE, s_dilution_3y DOUBLE,
        s_eps_rev_3m DOUBLE, s_net_revisions DOUBLE,
        price DOUBLE,
        price_date DATE,
        market_cap_eur DOUBLE,
        ev_eur DOUBLE,
        traded_value_3m_eur DOUBLE,
        fy0_period_end DATE,
        statement_age_days INTEGER,
        statement_currency VARCHAR,
        fx_rate_used DOUBLE,
        n_required INTEGER,
        n_missing INTEGER,
        missing_fields VARCHAR,
        missing_metrics VARCHAR,
        coverage_ratio DOUBLE,
        flags VARCHAR,
        PRIMARY KEY (run_id, isin)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dossiers (
        dossier_id VARCHAR PRIMARY KEY,   -- {as_of}_{isin}_{horodatage}
        as_of DATE NOT NULL,
        isin VARCHAR NOT NULL,
        ticker VARCHAR,
        name VARCHAR,
        run_id VARCHAR,                   -- classement d'où vient la sélection
        motif VARCHAR NOT NULL,           -- top60 | bond | detenue
        rank INTEGER,
        total_score DOUBLE,
        statut VARCHAR NOT NULL,          -- valide | rejete | echec
        motifs_rejet VARCHAR,
        conviction INTEGER,
        horizon_mois INTEGER,
        contenu VARCHAR,                  -- le dossier, en JSON
        faits VARCHAR,                    -- le dossier de travail fourni au modèle, en JSON
        versions_prompts VARCHAR,
        modele_extraction VARCHAR,
        modele_synthese VARCHAR,
        cout_eur DOUBLE,
        duree_s DOUBLE,
        git_sha VARCHAR,
        genere_le_utc TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_calls (
        dossier_id VARCHAR NOT NULL,
        etape VARCHAR NOT NULL,           -- extraction | debat | synthese
        modele VARCHAR NOT NULL,
        tokens_entree INTEGER,
        tokens_sortie INTEGER,
        cout_eur DOUBLE,
        duree_s DOUBLE,
        appele_le_utc TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS news (
        ticker VARCHAR NOT NULL,
        url VARCHAR NOT NULL,
        titre VARCHAR NOT NULL,
        resume VARCHAR,
        editeur VARCHAR,
        publie_le DATE,
        fetched_at_utc TIMESTAMP NOT NULL,
        PRIMARY KEY (ticker, url)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS coverage (
        run_id VARCHAR NOT NULL,
        population VARCHAR NOT NULL,          -- universe|scored
        field VARCHAR NOT NULL,
        n_present INTEGER NOT NULL,
        n_missing INTEGER NOT NULL,
        PRIMARY KEY (run_id, population, field)
    )
    """,
)

TABLES: tuple[str, ...] = (
    "schema_version",
    "universe_files",
    "listings",
    "universe",
    "universe_membership",
    "prices",
    "fx_rates",
    "statements",
    "earnings_dates",
    "descriptors",
    "consensus",
    "fetch_log",
    "runs",
    "scores",
    "coverage",
    "dossiers",
    "llm_calls",
    "news",
)


def now_utc() -> dt.datetime:
    """Horodatage UTC naïf (DuckDB stocke des TIMESTAMP sans fuseau).

    La microseconde est conservée : cet horodatage sert d'estampille de version pour les
    états financiers et les instantanés de métadonnées, deux écritures rapprochées ne
    doivent pas se confondre.
    """
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def connect(db_path: Path | str, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Ouvre la base et applique le DDL (idempotent). Lève duckdb.IOException si verrouillée."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=read_only)
    con.execute("SET TimeZone='UTC'")
    if not read_only:
        for statement in DDL:
            con.execute(statement)
        recorded = con.execute("SELECT max(version) FROM schema_version").fetchone()[0]
        if recorded is None or recorded < SCHEMA_VERSION:
            # Le schéma ne fait que s'enrichir : les nouvelles tables viennent d'être créées
            # par le DDL ci-dessus, il suffit d'enregistrer la version atteinte.
            con.execute("INSERT INTO schema_version VALUES (?, ?)", [SCHEMA_VERSION, now_utc()])
        elif recorded > SCHEMA_VERSION:
            raise RuntimeError(
                f"Base en version de schéma {recorded}, code en version {SCHEMA_VERSION}. "
                "Le code est plus ancien que la base : mets-le à jour."
            )
    return con


def insert_df(
    con: duckdb.DuckDBPyConnection,
    table: str,
    df: pd.DataFrame,
    *,
    on_conflict: str = "ignore",
) -> int:
    """Insère un DataFrame dans une table existante. on_conflict : ignore | replace | error.

    Les colonnes du DataFrame doivent exister dans la table ; celles qui manquent restent NULL.
    Renvoie le nombre de lignes envoyées.
    """
    if df.empty:
        return 0
    if on_conflict not in {"ignore", "replace", "error"}:
        raise ValueError(f"on_conflict inconnu : {on_conflict}")

    table_cols = [
        row[0] for row in con.execute(f"SELECT column_name FROM (DESCRIBE {table})").fetchall()
    ]
    unknown = [c for c in df.columns if c not in table_cols]
    if unknown:
        raise ValueError(f"colonnes inconnues pour {table} : {unknown}")

    payload = _cast_to_table_types(con, table, df)
    cols = ", ".join(f'"{c}"' for c in payload.columns)
    keys = _primary_key(con, table)
    con.register("_payload", payload)
    try:
        if not keys:
            # Sans clé primaire, DuckDB refuse une clause ON CONFLICT : c'est une table
            # d'ajout pur, comme le journal des appels au modèle.
            suffix = ""
        elif on_conflict == "ignore":
            suffix = " ON CONFLICT DO NOTHING"
        elif on_conflict == "replace":
            updates = [c for c in payload.columns if c not in keys]
            if not updates:
                suffix = " ON CONFLICT DO NOTHING"
            else:
                assignments = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in updates)
                key_list = ", ".join(f'"{k}"' for k in keys)
                suffix = f" ON CONFLICT ({key_list}) DO UPDATE SET {assignments}"
        else:
            suffix = ""
        con.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM _payload{suffix}")
    finally:
        con.unregister("_payload")
    return len(payload)


def _cast_to_table_types(
    con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame
) -> pd.DataFrame:
    """Aligne les colonnes sur les types déclarés de la table.

    Sans cela DuckDB déduit le type d'après les premières lignes : un volume de quatre
    milliards d'actions, après une série de petits nombres, faisait échouer l'insertion.
    """
    types = {
        row[0]: row[1].upper()
        for row in con.execute(f"SELECT column_name, column_type FROM (DESCRIBE {table})").fetchall()
    }
    payload = pd.DataFrame(index=df.index)
    for column in df.columns:
        serie = df[column]
        declared = types.get(column, "")
        if declared in ("BIGINT", "INTEGER", "HUGEINT", "SMALLINT"):
            payload[column] = pd.to_numeric(serie, errors="coerce").astype("Int64")
        elif declared in ("DOUBLE", "FLOAT", "DECIMAL", "REAL"):
            payload[column] = pd.to_numeric(serie, errors="coerce").astype("Float64")
        elif declared == "BOOLEAN":
            payload[column] = serie.map(lambda v: None if pd.isna(v) else bool(v)).astype("boolean")
        elif declared in ("DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE"):
            payload[column] = serie.map(lambda v: None if v is None or pd.isna(v) else v)
        else:
            payload[column] = serie.map(
                lambda v: None if v is None or (not isinstance(v, str) and pd.isna(v)) else str(v)
            )
    return payload


def _primary_key(con: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    rows = con.execute(
        """
        SELECT constraint_column_names
        FROM duckdb_constraints()
        WHERE table_name = ? AND constraint_type = 'PRIMARY KEY'
        """,
        [table],
    ).fetchall()
    return list(rows[0][0]) if rows else []
