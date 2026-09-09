"""Interface minimale d'accès aux données de marché.

Une seule implémentation existe (Yahoo, dans yahoo.py). Ce protocole n'est là que pour
permettre de brancher un autre fournisseur plus tard sans réécrire le reste : les noms de
colonnes des DataFrame renvoyés sont le contrat.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol

import pandas as pd


class ProviderError(Exception):
    """Échec d'accès au fournisseur de données."""


class NotFound(ProviderError):
    """Le fournisseur ne connaît pas ce symbole."""


class RateLimited(ProviderError):
    """Le fournisseur limite le débit et n'a pas cédé après les temporisations."""


@dataclass(frozen=True)
class Resolution:
    ticker: str | None
    method: str  # mnemonic | lookup | manual | unresolved
    descriptors: dict | None = None


# Colonnes attendues, dans cet ordre, pour chaque méthode.
PRICE_COLUMNS = ("date", "open", "high", "low", "close", "adj_close", "volume", "dividend", "split_ratio")
FX_COLUMNS = ("date", "rate")
STATEMENT_COLUMNS = ("statement", "period_type", "period_end", "field", "value", "currency")
EARNINGS_COLUMNS = ("event_date", "eps_estimate", "eps_reported")

DESCRIPTOR_KEYS = (
    "long_name", "sector", "industry", "country", "quote_type", "exchange",
    "quote_currency", "financial_currency", "shares_outstanding", "market_cap", "avg_volume_3m",
)
CONSENSUS_KEYS = (
    "currency",
    "eps_0y_current", "eps_0y_7d", "eps_0y_30d", "eps_0y_60d", "eps_0y_90d",
    "eps_1y_current", "eps_1y_7d", "eps_1y_30d", "eps_1y_60d", "eps_1y_90d",
    "up_30d_0y", "down_30d_0y", "up_30d_1y", "down_30d_1y",
    "n_analysts_0y", "n_analysts_1y",
)


class DataProvider(Protocol):
    """Toutes les méthodes lèvent NotFound si le symbole est inconnu, RateLimited si bloqué."""

    def resolve(self, isin: str, mnemonic: str | None, mic: str) -> Resolution:
        """Associe un ISIN à un symbole du fournisseur."""

    def prices(self, ticker: str, start: dt.date | None) -> pd.DataFrame:
        """Cours bruts (non ajustés), dividendes et splits. Colonnes : PRICE_COLUMNS."""

    def fx(self, quote_ccy: str, start: dt.date | None) -> pd.DataFrame:
        """Taux de change quotidiens : 1 EUR = rate quote_ccy. Colonnes : FX_COLUMNS."""

    def statements(self, ticker: str) -> pd.DataFrame:
        """États financiers en format long. Colonnes : STATEMENT_COLUMNS."""

    def descriptors(self, ticker: str) -> dict:
        """Métadonnées de la société. Clés : DESCRIPTOR_KEYS."""

    def consensus(self, ticker: str) -> dict:
        """Consensus des analystes. Clés : CONSENSUS_KEYS."""

    def earnings_dates(self, ticker: str) -> pd.DataFrame:
        """Dates de publication passées. Colonnes : EARNINGS_COLUMNS."""
