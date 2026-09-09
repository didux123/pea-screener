"""Accès aux données Yahoo Finance : limitation de débit, cache disque, normalisation.

Seule implémentation du protocole DataProvider. Deux principes :
  - toute réponse brute est écrite sur disque avant traitement, ce qui rend une reprise
    gratuite et permet de reconstruire la base sans retélécharger ;
  - les fonctions de normalisation sont pures et testées sur des réponses figées.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path

import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFRateLimitError

from pea.data.provider import (
    CONSENSUS_KEYS,
    EARNINGS_COLUMNS,
    FX_COLUMNS,
    PRICE_COLUMNS,
    STATEMENT_COLUMNS,
    NotFound,
    RateLimited,
    Resolution,
)
from pea.universe import MIC_TO_YF_SUFFIX

log = logging.getLogger(__name__)

BACKOFF_SECONDS = (120, 240, 480)

# Correspondance entre le suffixe du symbole et le code de place que Yahoo renvoie.
SUFFIX_TO_YF_EXCHANGES: dict[str, frozenset[str]] = {
    "PA": frozenset({"PAR", "PSE"}),
    "AS": frozenset({"AMS"}),
    "BR": frozenset({"BRU", "EBR"}),
    "LS": frozenset({"LIS"}),
    "DE": frozenset({"GER", "XETRA", "ETR"}),
}

_STATEMENT_METHODS = {
    "income": "get_income_stmt",
    "balance": "get_balance_sheet",
    "cashflow": "get_cashflow",
}
# Seuls les exercices annuels sont récupérés : les états trimestriels sont vides pour les
# sociétés à publication semestrielle, et les mélanger fausserait les classements par
# secteur. Les récupérer coûterait trois requêtes par valeur pour des données inutilisées.
_PERIOD_TYPES = {"annual": "yearly"}


class Pacer:
    """Espace les requêtes et marque une pause régulière, comme Yahoo l'exige.

    L'horloge et la temporisation sont injectables pour que les tests n'attendent pas.
    """

    def __init__(self, limits, *, sleep=time.sleep, clock=time.monotonic):
        self.requests_per_pause = limits.requests_per_pause
        self.pause_seconds = limits.pause_seconds
        self.min_interval_seconds = limits.min_interval_seconds
        self._sleep = sleep
        self._clock = clock
        self._count = 0
        self._last: float | None = None

    def tick(self) -> None:
        if self._count and self._count % self.requests_per_pause == 0:
            log.info("Pause de %.0f s après %d requêtes", self.pause_seconds, self._count)
            self._sleep(self.pause_seconds)
            self._last = None
        if self._last is not None:
            waiting = self.min_interval_seconds - (self._clock() - self._last)
            if waiting > 0:
                self._sleep(waiting)
        self._last = self._clock()
        self._count += 1

    def backoff(self, attempt: int) -> None:
        """Temporisation croissante après un blocage. Lève RateLimited à la fin."""
        if attempt >= len(BACKOFF_SECONDS):
            raise RateLimited("Yahoo limite le débit après trois temporisations")
        delay = BACKOFF_SECONDS[attempt]
        log.warning("Débit limité par Yahoo, attente de %d s", delay)
        self._sleep(delay)
        self._last = None


# ------------------------------------------------------------------------- normalisation


def _as_date(value) -> dt.date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert(None) if stamp.tz is not None else stamp.tz_localize(None)
    return stamp.date()


def _as_float(value) -> float | None:
    if value is None or value == "" or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def _as_int(value) -> int | None:
    number = _as_float(value)
    return None if number is None else int(number)


def normalize_history(raw: pd.DataFrame) -> pd.DataFrame:
    """Cours bruts. L'index horodaté devient une date ; aucune valeur n'est imputée."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=list(PRICE_COLUMNS))
    frame = raw.copy()
    index = pd.DatetimeIndex(frame.index)
    if index.tz is not None:
        index = index.tz_localize(None)
    frame.index = index
    renamed = {
        "Open": "open", "High": "high", "Low": "low", "Close": "close",
        "Adj Close": "adj_close", "Volume": "volume",
        "Dividends": "dividend", "Stock Splits": "split_ratio",
    }
    frame = frame.rename(columns=renamed)
    for column in PRICE_COLUMNS:
        if column not in frame.columns and column != "date":
            frame[column] = pd.NA
    frame = frame.reset_index(names="date")
    frame["date"] = frame["date"].dt.date
    frame["dividend"] = frame["dividend"].fillna(0.0)
    frame["split_ratio"] = frame["split_ratio"].fillna(0.0)
    frame = frame[list(PRICE_COLUMNS)]
    return frame.dropna(subset=["close"]).reset_index(drop=True)


def normalize_fx(raw: pd.DataFrame) -> pd.DataFrame:
    """Taux quotidiens : la clôture de EUR{CCY}=X donne 1 EUR = rate {CCY}."""
    prices = normalize_history(raw)
    if prices.empty:
        return pd.DataFrame(columns=list(FX_COLUMNS))
    return prices[["date", "close"]].rename(columns={"close": "rate"})


def normalize_statement(raw: pd.DataFrame, statement: str, period_type: str, currency: str | None):
    """Passe un état financier au format long. Une valeur absente ne crée pas de ligne."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=list(STATEMENT_COLUMNS))
    records: list[dict] = []
    for column in raw.columns:
        period_end = _as_date(column)
        if period_end is None:
            continue
        for field, value in raw[column].items():
            number = _as_float(value)
            if number is None:
                continue  # champ absent : il le reste, il n'est jamais remplacé par zéro
            records.append(
                {
                    "statement": statement,
                    "period_type": period_type,
                    "period_end": period_end,
                    "field": str(field),
                    "value": number,
                    "currency": currency,
                }
            )
    return pd.DataFrame(records, columns=list(STATEMENT_COLUMNS))


def _texte(value) -> str | None:
    """Une chaîne vide n'est pas une valeur : Yahoo renvoie parfois un secteur vide,
    qui formerait un faux groupe de comparaison au moment du classement."""
    if not isinstance(value, str):
        return None
    nettoye = value.strip()
    return nettoye or None


def normalize_info(raw: dict | None) -> dict:
    """Métadonnées. Une clé absente ou vide vaut None, jamais une valeur inventée."""
    raw = raw or {}
    return {
        "long_name": _texte(raw.get("longName")) or _texte(raw.get("shortName")),
        "sector": _texte(raw.get("sector")),
        "industry": _texte(raw.get("industry")),
        "country": _texte(raw.get("country")),
        "quote_type": _texte(raw.get("quoteType")),
        "exchange": _texte(raw.get("exchange")),
        "quote_currency": _texte(raw.get("currency")),
        "financial_currency": _texte(raw.get("financialCurrency")),
        "shares_outstanding": _as_float(raw.get("sharesOutstanding")),
        "market_cap": _as_float(raw.get("marketCap")),
        "avg_volume_3m": _as_float(raw.get("averageDailyVolume3Month")),
    }


def normalize_consensus(
    eps_trend: pd.DataFrame | None,
    eps_revisions: pd.DataFrame | None,
    earnings_estimate: pd.DataFrame | None,
) -> dict:
    """Consensus par horizon (exercice en cours 0y, exercice suivant +1y).

    Yahoo écrit 0.0 quand une estimation passée n'existe pas : cette valeur est traitée
    comme absente, sans quoi la variation du consensus serait aberrante.
    """
    result: dict = {key: None for key in CONSENSUS_KEYS}

    def cell(frame, row, column):
        if frame is None or getattr(frame, "empty", True):
            return None
        if row not in frame.index or column not in frame.columns:
            return None
        return _as_float(frame.at[row, column])

    for horizon, row in (("0y", "0y"), ("1y", "+1y")):
        for suffix, column in (
            ("current", "current"), ("7d", "7daysAgo"), ("30d", "30daysAgo"),
            ("60d", "60daysAgo"), ("90d", "90daysAgo"),
        ):
            value = cell(eps_trend, row, column)
            if value == 0.0:
                value = None  # convention Yahoo : 0 signifie « pas d'estimation »
            result[f"eps_{horizon}_{suffix}"] = value
        result[f"up_30d_{horizon}"] = _as_int(cell(eps_revisions, row, "upLast30days"))
        result[f"down_30d_{horizon}"] = _as_int(cell(eps_revisions, row, "downLast30days"))
        result[f"n_analysts_{horizon}"] = _as_int(cell(earnings_estimate, row, "numberOfAnalysts"))

    for frame in (eps_trend, earnings_estimate):
        if frame is not None and not getattr(frame, "empty", True) and "currency" in frame.columns:
            values = [v for v in frame["currency"].tolist() if isinstance(v, str) and v]
            if values:
                result["currency"] = values[0]
                break
    return result


def normalize_earnings_dates(raw: pd.DataFrame | None, today: dt.date) -> pd.DataFrame:
    """Dates de publication déjà passées, seules utilisables pour dater un état."""
    if raw is None or getattr(raw, "empty", True):
        return pd.DataFrame(columns=list(EARNINGS_COLUMNS))
    records = []
    for stamp, row in raw.iterrows():
        event_date = _as_date(stamp)
        if event_date is None or event_date > today:
            continue
        records.append(
            {
                "event_date": event_date,
                "eps_estimate": _as_float(row.get("EPS Estimate")),
                "eps_reported": _as_float(row.get("Reported EPS")),
            }
        )
    frame = pd.DataFrame(records, columns=list(EARNINGS_COLUMNS))
    return frame.drop_duplicates(subset=["event_date"], keep="first")


# ---------------------------------------------------------------------------- fournisseur


class YahooProvider:
    """Implémentation Yahoo du protocole DataProvider."""

    def __init__(self, cache_dir: Path, pacer: Pacer, *, today: dt.date | None = None):
        self.cache_dir = Path(cache_dir) / "yahoo"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.pacer = pacer
        self.today = today or dt.date.today()
        self.last_cache_path: str | None = None
        tz_cache = Path(cache_dir) / "yfinance_tz"
        tz_cache.mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(str(tz_cache))

    # -- cache ------------------------------------------------------------------

    def _path(self, endpoint: str, key: str, suffix: str) -> Path:
        safe = key.replace("/", "_").replace(" ", "_")
        directory = self.cache_dir / endpoint / safe
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{self.today:%Y-%m-%d}{suffix}"

    def _cached_frame(self, endpoint: str, key: str, fetch) -> pd.DataFrame:
        path = self._path(endpoint, key, ".parquet")
        self.last_cache_path = str(path)
        if path.is_file():
            return pd.read_parquet(path)
        frame = self._call(fetch)
        if frame is None:
            frame = pd.DataFrame()
        frame_to_store = frame.copy()
        # Parquet exige des noms de colonnes textuels ; les valeurs gardent leur type.
        frame_to_store.columns = [_column_label(c) for c in frame_to_store.columns]
        frame_to_store.reset_index().to_parquet(path, index=False)
        # On relit depuis le cache pour que le chemin « réseau » et le chemin « cache »
        # donnent rigoureusement le même résultat.
        return pd.read_parquet(path)

    def _cached_json(self, endpoint: str, key: str, fetch) -> dict:
        path = self._path(endpoint, key, ".json")
        self.last_cache_path = str(path)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        payload = self._call(fetch) or {}
        path.write_text(json.dumps(payload, default=str, ensure_ascii=False), encoding="utf-8")
        return payload

    def _call(self, fetch):
        """Exécute un appel réseau sous limiteur, avec temporisation en cas de blocage."""
        attempt = 0
        while True:
            self.pacer.tick()
            try:
                return fetch()
            except YFRateLimitError:
                self.pacer.backoff(attempt)
                attempt += 1

    # -- protocole --------------------------------------------------------------

    def resolve(self, isin: str, mnemonic: str | None, mic: str) -> Resolution:
        suffix = MIC_TO_YF_SUFFIX.get(mic)
        if suffix is None:
            return Resolution(ticker=None, method="unresolved")

        if mnemonic:
            candidate = f"{mnemonic}.{suffix}"
            info = self._raw_info(candidate)
            if self._info_matches(info, suffix):
                return Resolution(candidate, "mnemonic", normalize_info(info))

        table = self._lookup(isin)
        for symbol in table:
            if symbol.upper().endswith(f".{suffix}"):
                info = self._raw_info(symbol)
                if self._info_matches(info, suffix):
                    return Resolution(symbol, "lookup", normalize_info(info))
        return Resolution(ticker=None, method="unresolved")

    def prices(self, ticker: str, start: dt.date | None) -> pd.DataFrame:
        raw = self._history(ticker, start)
        frame = normalize_history(raw)
        if frame.empty and start is None:
            raise NotFound(f"aucun cours pour {ticker}")
        return frame

    def fx(self, quote_ccy: str, start: dt.date | None) -> pd.DataFrame:
        pair = f"EUR{quote_ccy.upper()}=X"
        raw = self._history(pair, start, endpoint="fx")
        frame = normalize_fx(raw)
        if frame.empty and start is None:
            raise NotFound(f"aucun taux pour {pair}")
        return frame

    def statements(self, ticker: str) -> pd.DataFrame:
        currency = normalize_info(self._raw_info(ticker)).get("financial_currency")
        frames = []
        for statement, method in _STATEMENT_METHODS.items():
            for period_type, freq in _PERIOD_TYPES.items():
                raw = self._cached_frame(
                    f"{statement}_{period_type}",
                    ticker,
                    lambda t=ticker, m=method, f=freq: getattr(yf.Ticker(t), m)(pretty=False, freq=f),
                )
                frames.append(
                    normalize_statement(_restore_frame(raw), statement, period_type, currency)
                )
        result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if result.empty:
            raise NotFound(f"aucun état financier pour {ticker}")
        return result

    def descriptors(self, ticker: str) -> dict:
        info = self._raw_info(ticker)
        if not self._looks_like_a_quote(info):
            raise NotFound(f"métadonnées absentes pour {ticker}")
        return normalize_info(info)

    def consensus(self, ticker: str) -> dict:
        raw = self._cached_json("analysis", ticker, lambda: self._fetch_analysis(ticker))
        return normalize_consensus(
            _frame_from_json(raw.get("eps_trend")),
            _frame_from_json(raw.get("eps_revisions")),
            _frame_from_json(raw.get("earnings_estimate")),
        )

    def earnings_dates(self, ticker: str) -> pd.DataFrame:
        raw = self._cached_frame(
            "earnings_dates",
            ticker,
            lambda: yf.Ticker(ticker).get_earnings_dates(limit=60),
        )
        return normalize_earnings_dates(_restore_frame(raw, index_is_date=True), self.today)

    # -- appels bruts -----------------------------------------------------------

    def _history(self, symbol: str, start: dt.date | None, *, endpoint: str = "history"):
        key = f"{symbol}_{start:%Y%m%d}" if start else f"{symbol}_max"
        raw = self._cached_frame(
            endpoint,
            key,
            lambda: yf.Ticker(symbol).history(
                start=start.isoformat() if start else None,
                period=None if start else "max",
                auto_adjust=False,
                actions=True,
            ),
        )
        return _restore_frame(raw, index_is_date=True)

    def _raw_info(self, ticker: str) -> dict:
        return self._cached_json("info", ticker, lambda: yf.Ticker(ticker).get_info())

    def _lookup(self, isin: str) -> list[str]:
        raw = self._cached_json("lookup", isin, lambda: self._fetch_lookup(isin))
        return list(raw.get("symbols", []))

    def _fetch_lookup(self, isin: str) -> dict:
        try:
            table = yf.Lookup(isin).get_stock(count=10)
        except YFRateLimitError:
            raise
        except Exception as exc:
            log.debug("Lookup %s : %s", isin, exc)
            return {"symbols": []}
        if table is None or getattr(table, "empty", True):
            return {"symbols": []}
        return {"symbols": [str(s) for s in table.index]}

    def _fetch_analysis(self, ticker: str) -> dict:
        handle = yf.Ticker(ticker)
        payload = {}
        for name in ("eps_trend", "eps_revisions", "earnings_estimate"):
            try:
                frame = getattr(handle, name)
            except YFRateLimitError:
                raise
            except Exception as exc:
                log.debug("%s %s : %s", ticker, name, exc)
                frame = None
            payload[name] = _frame_to_json(frame)
        return payload

    @staticmethod
    def _looks_like_a_quote(info: dict | None) -> bool:
        # Yahoo répond {'trailingPegRatio': None} pour un symbole inconnu.
        return bool(info) and any(info.get(key) for key in ("quoteType", "exchange", "longName"))

    @classmethod
    def _info_matches(cls, info: dict | None, suffix: str) -> bool:
        if not cls._looks_like_a_quote(info):
            return False
        if (info.get("quoteType") or "").upper() != "EQUITY":
            return False
        exchange = (info.get("exchange") or "").upper()
        expected = SUFFIX_TO_YF_EXCHANGES.get(suffix)
        return not expected or not exchange or exchange in expected


# -------------------------------------------------------- sérialisation du cache brut


def _frame_to_json(frame) -> dict | None:
    if frame is None or getattr(frame, "empty", True):
        return None
    return {
        "index": [str(i) for i in frame.index],
        "columns": [str(c) for c in frame.columns],
        "data": [[None if pd.isna(v) else v for v in row] for row in frame.to_numpy().tolist()],
    }


def _frame_from_json(payload: dict | None):
    if not payload:
        return None
    return pd.DataFrame(payload["data"], index=payload["index"], columns=payload["columns"])


def _column_label(value) -> str:
    """Les colonnes d'un état financier sont des dates de clôture : on les écrit en ISO."""
    if isinstance(value, pd.Timestamp | dt.date):
        return pd.Timestamp(value).strftime("%Y-%m-%dT%H:%M:%S")
    return str(value)


def _restore_frame(raw: pd.DataFrame, *, index_is_date: bool = False) -> pd.DataFrame:
    """Reconstruit un tableau lu depuis le cache : première colonne = ancien index."""
    if raw is None or raw.empty:
        return pd.DataFrame()
    frame = raw.copy()
    frame = frame.set_index(frame.columns[0])
    frame.index.name = None
    if index_is_date:
        frame.index = pd.to_datetime(frame.index, format="mixed", errors="coerce")
        frame = frame[frame.index.notna()]
    else:
        frame.columns = [
            pd.Timestamp(c) if _looks_like_a_date(c) else c for c in frame.columns
        ]
    return frame


def _looks_like_a_date(value) -> bool:
    if not isinstance(value, str) or len(value) < 10 or value[4] != "-":
        return False
    try:
        pd.Timestamp(value)
    except (ValueError, TypeError):
        return False
    return True
