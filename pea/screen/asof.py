"""Accès aux données telles qu'elles étaient connues à une date donnée.

Règle unique : aucune information postérieure à `as_of`, et aucune ligne récupérée après
la fin de cette journée, n'entre dans un calcul portant sur cette date. Chaque chargement
renvoie aussi de quoi vérifier cette propriété après coup (voir Audit).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

# Délais de publication réglementaires maximaux (directive Transparence). Ils servent
# seulement quand la date de publication réelle est inconnue : le retard est prudent.
PUBLICATION_LAG_DAYS = {"annual": 120, "semiannual": 90, "quarterly": 60}
EARNINGS_DATE_FLOOR_DAYS = 14   # une date trop proche de la clôture n'est pas la publication
EARNINGS_DATE_MARGIN_DAYS = 30  # tolérance au-delà du délai réglementaire
CONSENSUS_MAX_AGE_DAYS = 30     # au-delà, le consensus est considéré comme absent
PRICE_LOOKBACK_DAYS = 420       # de quoi calculer un momentum 12 mois et une moyenne 200 jours
FX_MAX_AGE_DAYS = 10


@dataclass
class Audit:
    """De quoi prouver qu'aucune donnée postérieure n'a été utilisée."""

    max_fetched_at_used: dt.datetime | None = None
    max_info_date_used: dt.date | None = None

    def note_fetched(self, value) -> None:
        if value is None or pd.isna(value):
            return
        stamp = pd.Timestamp(value).to_pydatetime()
        if self.max_fetched_at_used is None or stamp > self.max_fetched_at_used:
            self.max_fetched_at_used = stamp

    def note_info(self, value) -> None:
        if value is None or pd.isna(value):
            return
        day = pd.Timestamp(value).date()
        if self.max_info_date_used is None or day > self.max_info_date_used:
            self.max_info_date_used = day

    def note_frame(self, frame: pd.DataFrame, *, fetched="fetched_at_utc", info=None) -> None:
        if frame is None or frame.empty:
            return
        if fetched and fetched in frame.columns:
            self.note_fetched(frame[fetched].max())
        if info and info in frame.columns:
            self.note_info(frame[info].max())


@dataclass
class PITData:
    """Photographie de la base à une date, prête pour le calcul des métriques."""

    as_of: dt.date
    cutoff: dt.datetime
    mode: str                     # live | reconstructed
    survivorship_complete: bool
    universe: pd.DataFrame
    statements: pd.DataFrame
    prices: pd.DataFrame
    fx: pd.DataFrame
    descriptors: pd.DataFrame
    consensus: pd.DataFrame
    audit: Audit = field(default_factory=Audit)


def _as_dates(frame: pd.DataFrame, *columns: str) -> pd.DataFrame:
    """DuckDB renvoie des horodatages ; le calcul des métriques raisonne en dates."""
    for column in columns:
        if column in frame.columns and not frame.empty:
            frame[column] = frame[column].map(lambda v: None if pd.isna(v) else pd.Timestamp(v).date())
    return frame


def cutoff_utc(as_of: dt.date) -> dt.datetime:
    """Fin de la journée `as_of` : une donnée récupérée après n'existait pas encore."""
    return dt.datetime.combine(as_of + dt.timedelta(days=1), dt.time())


def first_ingest_date(con) -> dt.date | None:
    row = con.execute(
        "SELECT min(CAST(fetched_at_utc AS DATE)) FROM fetch_log WHERE status IN ('ok', 'empty')"
    ).fetchone()
    return row[0] if row else None


def detect_mode(con, as_of: dt.date) -> str:
    """`live` si le système tournait déjà à cette date, `reconstructed` sinon."""
    debut = first_ingest_date(con)
    return "live" if debut is not None and as_of >= debut else "reconstructed"


def publication_lag(period_type: str) -> int:
    return PUBLICATION_LAG_DAYS.get(period_type, PUBLICATION_LAG_DAYS["annual"])


def published_at_for(
    period_end: dt.date, period_type: str, event_dates: list[dt.date]
) -> dt.date | None:
    """Date de publication réelle d'un exercice, si une date de résultats correspond."""
    if not event_dates:
        return None
    debut = period_end + dt.timedelta(days=EARNINGS_DATE_FLOOR_DAYS)
    fin = period_end + dt.timedelta(days=publication_lag(period_type) + EARNINGS_DATE_MARGIN_DAYS)
    candidates = [d for d in event_dates if debut <= d <= fin]
    return min(candidates) if candidates else None


def available_from(
    period_end: dt.date,
    period_type: str,
    first_fetched_date: dt.date | None,
    published_at: dt.date | None,
) -> dt.date:
    """Date à partir de laquelle un état était connaissable.

    La première récupération fait foi quand elle est antérieure : si le système a vu la
    donnée le 3 mars, elle existait le 3 mars, quelle que soit l'estimation.
    """
    estimee = period_end + dt.timedelta(days=publication_lag(period_type))
    candidates = [published_at or estimee]
    if first_fetched_date is not None:
        candidates.append(first_fetched_date)
    return min(candidates)


# ------------------------------------------------------------------------------ chargements


def universe_at(con, as_of: dt.date, mode: str) -> pd.DataFrame:
    """Valeurs présentes dans l'univers à cette date, radiations comprises."""
    if mode == "live":
        return con.execute(
            """
            SELECT isin, name, mic, mnemonic, quote_currency, isin_country, yf_ticker, resolution
            FROM universe
            WHERE first_seen <= ? AND (delisted_at IS NULL OR delisted_at > ?)
            ORDER BY isin
            """,
            [as_of, as_of],
        ).df()
    # Avant le démarrage du système, l'appartenance ne peut être reconstituée que par la
    # présence de cours à cette date : l'univers est alors biaisé par la survie.
    return con.execute(
        """
        SELECT u.isin, u.name, u.mic, u.mnemonic, u.quote_currency, u.isin_country,
               u.yf_ticker, u.resolution
        FROM universe u
        WHERE (u.delisted_at IS NULL OR u.delisted_at > ?)
          AND (u.yf_ticker IS NULL
               OR EXISTS (SELECT 1 FROM prices p WHERE p.ticker = u.yf_ticker AND p.date <= ?))
        ORDER BY u.isin
        """,
        [as_of, as_of],
    ).df()


def statements_at(con, as_of: dt.date, cutoff: dt.datetime, *, period_type: str = "annual") -> pd.DataFrame:
    """États financiers connus à la date, une seule version par champ.

    On prend la dernière version récupérée avant la fin de la journée ; s'il n'en existe
    aucune (calcul rétrospectif), la plus ancienne, signalée par `from_later_fetch`.
    """
    frame = con.execute(
        """
        WITH versions AS (
            SELECT s.ticker, s.statement, s.period_type, s.period_end, s.field, s.value,
                   s.currency, s.fetched_at_utc,
                   min(s.fetched_at_utc) OVER (
                       PARTITION BY s.ticker, s.statement, s.period_end) AS first_fetched_at,
                   count(*) OVER (
                       PARTITION BY s.ticker, s.statement, s.period_end, s.field) AS n_versions,
                   row_number() OVER (
                       PARTITION BY s.ticker, s.statement, s.period_end, s.field
                       ORDER BY (s.fetched_at_utc >= ?),
                                CASE WHEN s.fetched_at_utc < ? THEN s.fetched_at_utc END DESC,
                                s.fetched_at_utc ASC) AS rang
            FROM statements s
            WHERE s.period_type = ?
        )
        SELECT ticker, statement, period_end, field, value, currency, fetched_at_utc,
               CAST(first_fetched_at AS DATE) AS first_fetched_date,
               n_versions, (fetched_at_utc >= ?) AS from_later_fetch
        FROM versions WHERE rang = 1
        """,
        [cutoff, cutoff, period_type, cutoff],
    ).df()
    if frame.empty:
        return frame.assign(
            available_from=pd.Series(dtype="object"),
            published_at_estimated=pd.Series(dtype="bool"),
        )

    publications = _publication_dates(con, cutoff)
    frame["period_end"] = frame["period_end"].map(lambda d: pd.Timestamp(d).date())
    frame["first_fetched_date"] = frame["first_fetched_date"].map(
        lambda d: None if pd.isna(d) else pd.Timestamp(d).date()
    )
    reelles, disponibles = [], []
    for ticker, period_end in zip(frame["ticker"], frame["period_end"], strict=True):
        publiee = published_at_for(period_end, period_type, publications.get(ticker, []))
        reelles.append(publiee)
        disponibles.append(publiee)
    frame["published_at"] = reelles
    frame["published_at_estimated"] = frame["published_at"].isna()
    frame["available_from"] = [
        available_from(pe, period_type, ffd, pa)
        for pe, ffd, pa in zip(
            frame["period_end"], frame["first_fetched_date"], frame["published_at"], strict=True
        )
    ]
    return frame[frame["available_from"] <= as_of].reset_index(drop=True)


def _publication_dates(con, cutoff: dt.datetime) -> dict[str, list[dt.date]]:
    rows = con.execute(
        "SELECT ticker, event_date FROM earnings_dates WHERE fetched_at_utc < ? ORDER BY ticker, event_date",
        [cutoff],
    ).fetchall()
    dates: dict[str, list[dt.date]] = {}
    for ticker, event_date in rows:
        dates.setdefault(ticker, []).append(event_date)
    return dates


def prices_until(con, as_of: dt.date, *, lookback_days: int = PRICE_LOOKBACK_DAYS) -> pd.DataFrame:
    """Cours jusqu'à la date incluse, sur une fenêtre suffisante pour le momentum."""
    frame = con.execute(
        """
        SELECT ticker, date, close, volume, dividend, split_ratio, fetched_at_utc
        FROM prices WHERE date <= ? AND date >= ?
        ORDER BY ticker, date
        """,
        [as_of, as_of - dt.timedelta(days=lookback_days)],
    ).df()
    return _as_dates(frame, "date")


def splits_until(con, as_of: dt.date) -> pd.DataFrame:
    """Tous les splits connus jusqu'à la date : ils alignent les nombres d'actions."""
    frame = con.execute(
        """
        SELECT ticker, date, split_ratio FROM prices
        WHERE date <= ? AND split_ratio > 0 ORDER BY ticker, date
        """,
        [as_of],
    ).df()
    return _as_dates(frame, "date")


def fx_at(con, as_of: dt.date, cutoff: dt.datetime) -> pd.DataFrame:
    """Dernier taux connu par devise, à moins de dix jours de la date."""
    frame = con.execute(
        """
        SELECT quote_ccy, date, rate, fetched_at_utc FROM (
            SELECT *, row_number() OVER (PARTITION BY quote_ccy ORDER BY date DESC) AS rang
            FROM fx_rates WHERE date <= ? AND date >= ? AND fetched_at_utc < ?
        ) WHERE rang = 1
        """,
        [as_of, as_of - dt.timedelta(days=FX_MAX_AGE_DAYS), cutoff],
    ).df()
    return _as_dates(frame, "date")


def descriptors_at(con, cutoff: dt.datetime) -> pd.DataFrame:
    """Dernier instantané de métadonnées antérieur au calcul.

    S'il n'en existe aucun (calcul rétrospectif), on prend le plus ancien connu et on le
    signale : le secteur d'une société ne change pratiquement jamais, mais l'exception
    doit rester visible.
    """
    return con.execute(
        """
        SELECT ticker, fetched_at_utc, long_name, sector, industry, country, quote_type,
               exchange, quote_currency, financial_currency, shares_outstanding, market_cap,
               avg_volume_3m, from_later_fetch
        FROM (
            SELECT *, (fetched_at_utc >= ?) AS from_later_fetch,
                   row_number() OVER (
                       PARTITION BY ticker
                       ORDER BY (fetched_at_utc >= ?),
                                CASE WHEN fetched_at_utc < ? THEN fetched_at_utc END DESC,
                                fetched_at_utc ASC) AS rang
            FROM descriptors
        ) WHERE rang = 1
        """,
        [cutoff, cutoff, cutoff],
    ).df()


def consensus_at(con, as_of: dt.date, cutoff: dt.datetime) -> pd.DataFrame:
    """Consensus le plus récent, à condition d'avoir moins de trente jours.

    Aucun historique de consensus n'existe avant le démarrage du système : pour un calcul
    rétrospectif, ce tableau est vide et la composante est déclarée incomplète.
    """
    return con.execute(
        """
        SELECT * EXCLUDE (rang) FROM (
            SELECT *, row_number() OVER (PARTITION BY ticker ORDER BY fetched_at_utc DESC) AS rang
            FROM consensus WHERE fetched_at_utc < ? AND CAST(fetched_at_utc AS DATE) >= ?
        ) WHERE rang = 1
        """,
        [cutoff, as_of - dt.timedelta(days=CONSENSUS_MAX_AGE_DAYS)],
    ).df()


def load_pit(con, as_of: dt.date) -> PITData:
    """Charge tout ce qui est nécessaire au classement, à cette date et à cette date seule."""
    cutoff = cutoff_utc(as_of)
    mode = detect_mode(con, as_of)
    audit = Audit()

    univers = universe_at(con, as_of, mode)
    etats = statements_at(con, as_of, cutoff)
    cours = prices_until(con, as_of)
    changes = fx_at(con, as_of, cutoff)
    descripteurs = descriptors_at(con, cutoff)
    consensus = consensus_at(con, as_of, cutoff)

    audit.note_frame(etats, info="period_end")
    audit.note_frame(cours, info="date")
    audit.note_frame(changes, info="date")
    audit.note_frame(descripteurs)
    audit.note_frame(consensus)

    return PITData(
        as_of=as_of,
        cutoff=cutoff,
        mode=mode,
        survivorship_complete=(mode == "live"),
        universe=univers,
        statements=etats,
        prices=cours,
        fx=changes,
        descriptors=descripteurs,
        consensus=consensus,
        audit=audit,
    )
