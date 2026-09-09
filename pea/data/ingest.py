"""Ingestion des données de marché : orchestration, reprise, journal des récupérations.

Ordre d'une soirée : cours, taux de change, puis fondamentaux des valeurs « dues ».
Chaque appel est journalisé dans fetch_log ; une relance le même jour ne coûte rien
puisque les réponses brutes sont en cache.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

import pandas as pd

from pea import db as db_module
from pea.data.provider import DESCRIPTOR_KEYS, NotFound, ProviderError, RateLimited

log = logging.getLogger(__name__)

PRICE_OVERLAP_DAYS = 7          # on redemande une semaine pour attraper les corrections
FUNDAMENTALS_MAX_AGE_DAYS = 7   # fondamentaux rafraîchis une fois par semaine
MAX_CONSECUTIVE_NOT_FOUND = 3   # au-delà, la valeur est mise de côté jusqu'au prochain univers
MAX_CONSECUTIVE_RATE_LIMITED = 3
FUNDAMENTAL_ENDPOINTS = ("info", "statements", "consensus", "earnings_dates")


@dataclass
class IngestStats:
    ok: int = 0
    empty: int = 0
    not_found: int = 0
    errors: int = 0
    rows: int = 0

    def as_dict(self) -> dict:
        return {"ok": self.ok, "vides": self.empty, "absents": self.not_found,
                "erreurs": self.errors, "lignes": self.rows}


class RateLimitGuard:
    """Compte les blocages consécutifs de Yahoo et interrompt l'ingestion au troisième.

    Chaque blocage est journalisé : le rapport dit ce qui n'a pas pu être récupéré.
    """

    def __init__(self, con, maximum: int = MAX_CONSECUTIVE_RATE_LIMITED):
        self.con = con
        self.maximum = maximum
        self.consecutive = 0

    def hit(self, ticker: str, endpoint: str, error: Exception) -> None:
        self.consecutive += 1
        log_fetch(self.con, ticker, endpoint, "rate_limited", error=error)
        if self.consecutive >= self.maximum:
            raise RateLimited(
                f"Yahoo a limité le débit sur {self.consecutive} valeurs de suite ; "
                "ingestion interrompue, le reste sera repris au prochain passage"
            ) from error

    def ok(self) -> None:
        self.consecutive = 0


@dataclass
class IngestSummary:
    prices: IngestStats = field(default_factory=IngestStats)
    fx: IngestStats = field(default_factory=IngestStats)
    fundamentals: IngestStats = field(default_factory=IngestStats)
    aborted: bool = False
    reason: str | None = None


# --------------------------------------------------------------------------------- journal


def log_fetch(con, ticker, endpoint, status, *, n_rows=None, error=None, cache_path=None) -> None:
    con.execute(
        """
        INSERT INTO fetch_log (ticker, endpoint, fetched_at_utc, status, n_rows, error, cache_path)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [ticker, endpoint, db_module.now_utc(), status, n_rows,
         (str(error)[:500] if error else None), cache_path],
    )


def _succeeded_today(con, ticker: str, endpoint: str, today: dt.date) -> bool:
    row = con.execute(
        """
        SELECT count(*) FROM fetch_log
        WHERE ticker = ? AND endpoint = ? AND status IN ('ok', 'empty')
          AND CAST(fetched_at_utc AS DATE) = ?
        """,
        [ticker, endpoint, today],
    ).fetchone()
    return bool(row[0])


# ------------------------------------------------------------------------------ descripteurs


def store_descriptors(con, ticker: str, descriptors: dict) -> bool:
    """Ajoute un instantané de métadonnées, seulement si un champ a changé."""
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


# ------------------------------------------------------------------------------ états financiers


def upsert_statements(con, ticker: str, frame: pd.DataFrame) -> int:
    """Écrit les états en ne créant une ligne que si la valeur a changé.

    Un retraitement laisse donc l'ancienne version en place : on saura toujours ce qui
    était publié à une date passée.
    """
    if frame.empty:
        return 0
    payload = frame.copy()
    payload["ticker"] = ticker
    payload["fetched_at_utc"] = db_module.now_utc()
    con.register("_statements", payload)
    inserted = con.execute(
        """
        INSERT INTO statements
            (ticker, statement, period_type, period_end, field, value, currency, fetched_at_utc)
        SELECT n.ticker, n.statement, n.period_type, n.period_end, n.field, n.value,
               n.currency, n.fetched_at_utc
        FROM _statements n
        LEFT JOIN (
            SELECT ticker, statement, period_type, period_end, field, value,
                   row_number() OVER (
                       PARTITION BY ticker, statement, period_type, period_end, field
                       ORDER BY fetched_at_utc DESC) AS rang
            FROM statements WHERE ticker = ?
        ) d
          ON d.rang = 1 AND d.ticker = n.ticker AND d.statement = n.statement
         AND d.period_type = n.period_type AND d.period_end = n.period_end
         AND d.field = n.field AND d.value = n.value
        WHERE d.ticker IS NULL
        ON CONFLICT DO NOTHING
        """,
        [ticker],
    ).fetchall()
    con.unregister("_statements")
    return inserted[0][0] if inserted else 0


# ---------------------------------------------------------------------------------- cours


def _last_price_date(con, ticker: str) -> dt.date | None:
    return con.execute("SELECT max(date) FROM prices WHERE ticker = ?", [ticker]).fetchone()[0]


def ingest_prices(
    con, provider, tickers: list[str], today: dt.date, guard: "RateLimitGuard | None" = None
) -> IngestStats:
    """Cours quotidiens. Un split rend l'historique stocké caduc : il est rechargé en entier."""
    stats = IngestStats()
    guard = guard or RateLimitGuard(con)
    for ticker in tickers:
        if _succeeded_today(con, ticker, "prices", today):
            continue
        last = _last_price_date(con, ticker)
        start = (last - dt.timedelta(days=PRICE_OVERLAP_DAYS)) if last else None
        try:
            frame = provider.prices(ticker, start)
        except NotFound as exc:
            _mark_not_found(con, ticker)
            log_fetch(con, ticker, "prices", "not_found", error=exc,
                      cache_path=getattr(provider, "last_cache_path", None))
            stats.not_found += 1
            continue
        except RateLimited as exc:
            guard.hit(ticker, "prices", exc)
            continue
        except ProviderError as exc:
            log_fetch(con, ticker, "prices", "error", error=exc)
            stats.errors += 1
            continue

        if frame.empty:
            log_fetch(con, ticker, "prices", "empty", n_rows=0)
            stats.empty += 1
            continue

        # Un split survenu après la dernière date stockée invalide tout l'historique :
        # Yahoo rend des cours déjà ajustés, l'ancien et le nouveau ne sont plus comparables.
        if last is not None and (frame.loc[frame["date"] > last, "split_ratio"] > 0).any():
            log.info("%s : split détecté, rechargement complet de l'historique", ticker)
            frame = provider.prices(ticker, None)
            con.execute("DELETE FROM prices WHERE ticker = ?", [ticker])
            log_fetch(con, ticker, "prices", "price_reload_split", n_rows=len(frame))

        payload = frame.copy()
        payload["ticker"] = ticker
        payload["fetched_at_utc"] = db_module.now_utc()
        db_module.insert_df(con, "prices", payload, on_conflict="replace")
        _reset_not_found(con, ticker)
        log_fetch(con, ticker, "prices", "ok", n_rows=len(payload),
                  cache_path=getattr(provider, "last_cache_path", None))
        guard.ok()
        stats.ok += 1
        stats.rows += len(payload)
    return stats


def ingest_fx(con, provider, currencies: list[str], today: dt.date) -> IngestStats:
    """Taux de change des devises de publication rencontrées, hors euro."""
    stats = IngestStats()
    for currency in sorted({c.upper() for c in currencies if c and c.upper() != "EUR"}):
        if _succeeded_today(con, currency, "fx", today):
            continue
        last = con.execute(
            "SELECT max(date) FROM fx_rates WHERE quote_ccy = ?", [currency]
        ).fetchone()[0]
        start = (last - dt.timedelta(days=PRICE_OVERLAP_DAYS)) if last else None
        try:
            frame = provider.fx(currency, start)
        except NotFound as exc:
            log_fetch(con, currency, "fx", "not_found", error=exc)
            stats.not_found += 1
            continue
        except RateLimited as exc:
            log_fetch(con, currency, "fx", "rate_limited", error=exc)
            continue
        except ProviderError as exc:
            log_fetch(con, currency, "fx", "error", error=exc)
            stats.errors += 1
            continue
        if frame.empty:
            log_fetch(con, currency, "fx", "empty", n_rows=0)
            stats.empty += 1
            continue
        payload = frame.copy()
        payload["quote_ccy"] = currency
        payload["fetched_at_utc"] = db_module.now_utc()
        db_module.insert_df(con, "fx_rates", payload, on_conflict="replace")
        log_fetch(con, currency, "fx", "ok", n_rows=len(payload))
        stats.ok += 1
        stats.rows += len(payload)
    return stats


# --------------------------------------------------------------------------- fondamentaux


def due_tickers(con, today: dt.date, *, max_age_days: int = FUNDAMENTALS_MAX_AGE_DAYS) -> list[str]:
    """Valeurs dont les fondamentaux n'ont pas été rafraîchis depuis plus d'une semaine.

    Les plus anciennes d'abord, pour que le retard se résorbe seul.
    """
    rows = con.execute(
        """
        WITH actives AS (
            SELECT yf_ticker AS ticker FROM universe
            WHERE delisted_at IS NULL AND yf_ticker IS NOT NULL
              AND resolution <> 'lost' AND consecutive_not_found < ?
        ),
        derniers AS (
            SELECT ticker, endpoint, max(fetched_at_utc) AS quand
            FROM fetch_log WHERE status IN ('ok', 'empty') AND endpoint IN ('info', 'statements', 'consensus', 'earnings_dates')
            GROUP BY ticker, endpoint
        ),
        complet AS (
            SELECT ticker, min(quand) AS plus_ancien, count(*) AS n
            FROM derniers GROUP BY ticker
        )
        SELECT a.ticker
        FROM actives a LEFT JOIN complet c ON c.ticker = a.ticker
        WHERE c.ticker IS NULL OR c.n < 4 OR c.plus_ancien < ?
        ORDER BY coalesce(c.plus_ancien, TIMESTAMP '1970-01-01'), a.ticker
        """,
        [MAX_CONSECUTIVE_NOT_FOUND,
         dt.datetime.combine(today, dt.time()) - dt.timedelta(days=max_age_days)],
    ).fetchall()
    return [row[0] for row in rows]


def _mark_not_found(con, ticker: str) -> None:
    con.execute(
        """
        UPDATE universe SET consecutive_not_found = consecutive_not_found + 1,
            resolution = CASE WHEN consecutive_not_found + 1 >= ? THEN 'lost' ELSE resolution END
        WHERE yf_ticker = ?
        """,
        [MAX_CONSECUTIVE_NOT_FOUND, ticker],
    )


def _reset_not_found(con, ticker: str) -> None:
    con.execute(
        "UPDATE universe SET consecutive_not_found = 0 WHERE yf_ticker = ? AND consecutive_not_found > 0",
        [ticker],
    )


def ingest_fundamentals(
    con, provider, tickers: list[str], today: dt.date, guard: "RateLimitGuard | None" = None
) -> IngestStats:
    """Métadonnées, états financiers, consensus et dates de publication, valeur par valeur."""
    stats = IngestStats()
    guard = guard or RateLimitGuard(con)
    for ticker in tickers:
        introuvable = False
        for endpoint in FUNDAMENTAL_ENDPOINTS:
            if _succeeded_today(con, ticker, endpoint, today):
                continue
            try:
                n_rows = _fetch_one(con, provider, ticker, endpoint)
            except NotFound as exc:
                log_fetch(con, ticker, endpoint, "not_found", error=exc)
                introuvable = True
                break
            except RateLimited as exc:
                guard.hit(ticker, endpoint, exc)
                break
            except ProviderError as exc:
                log_fetch(con, ticker, endpoint, "error", error=exc)
                stats.errors += 1
                continue
            log_fetch(con, ticker, endpoint, "ok" if n_rows else "empty", n_rows=n_rows,
                      cache_path=getattr(provider, "last_cache_path", None))
            stats.rows += n_rows or 0
            guard.ok()
        if introuvable:
            _mark_not_found(con, ticker)
            stats.not_found += 1
        else:
            _reset_not_found(con, ticker)
            stats.ok += 1
    return stats


def _fetch_one(con, provider, ticker: str, endpoint: str) -> int:
    if endpoint == "info":
        descriptors = provider.descriptors(ticker)
        store_descriptors(con, ticker, descriptors)
        return 1
    if endpoint == "statements":
        return upsert_statements(con, ticker, provider.statements(ticker))
    if endpoint == "consensus":
        return _store_consensus(con, ticker, provider.consensus(ticker))
    if endpoint == "earnings_dates":
        frame = provider.earnings_dates(ticker)
        if frame.empty:
            return 0
        payload = frame.copy()
        payload["ticker"] = ticker
        payload["fetched_at_utc"] = db_module.now_utc()
        db_module.insert_df(con, "earnings_dates", payload, on_conflict="replace")
        return len(payload)
    raise ValueError(f"point d'accès inconnu : {endpoint}")


def _store_consensus(con, ticker: str, data: dict) -> int:
    if all(value is None for key, value in data.items() if key != "currency"):
        return 0  # aucune couverture analyste : rien à enregistrer
    row = {"ticker": ticker, "fetched_at_utc": db_module.now_utc(), **data}
    db_module.insert_df(con, "consensus", pd.DataFrame([row]), on_conflict="replace")
    return 1


# ------------------------------------------------------------------------------ orchestration


def active_tickers(con) -> list[str]:
    rows = con.execute(
        """
        SELECT yf_ticker FROM universe
        WHERE delisted_at IS NULL AND yf_ticker IS NOT NULL
          AND resolution <> 'lost' AND consecutive_not_found < ?
        ORDER BY yf_ticker
        """,
        [MAX_CONSECUTIVE_NOT_FOUND],
    ).fetchall()
    return [row[0] for row in rows]


def known_currencies(con) -> list[str]:
    rows = con.execute(
        """
        SELECT DISTINCT financial_currency FROM descriptors WHERE financial_currency IS NOT NULL
        UNION
        SELECT DISTINCT quote_currency FROM descriptors WHERE quote_currency IS NOT NULL
        """
    ).fetchall()
    return [row[0] for row in rows]


def run_ingest(con, provider, *, today: dt.date | None = None, limit: int | None = None) -> IngestSummary:
    """Une soirée d'ingestion : cours, taux, fondamentaux dus."""
    today = today or dt.date.today()
    summary = IngestSummary()
    tickers = active_tickers(con)
    if limit:
        tickers = tickers[:limit]
    guard = RateLimitGuard(con)
    try:
        summary.prices = ingest_prices(con, provider, tickers, today, guard)
        summary.fx = ingest_fx(con, provider, known_currencies(con) or ["USD"], today)
        dus = [t for t in due_tickers(con, today) if t in set(tickers)]
        summary.fundamentals = ingest_fundamentals(con, provider, dus, today, guard)
    except RateLimited as exc:
        summary.aborted = True
        summary.reason = str(exc)
        log.error("Ingestion interrompue : %s", exc)
    return summary
