"""Calcul des métriques d'une valeur à une date donnée. Fonctions pures, sans base.

Deux règles gouvernent tout ce module :
  - une donnée absente reste absente (None) et sera pénalisée au classement, jamais imputée ;
  - un dénominateur négatif ou nul n'est pas une donnée absente mais une information : la
    métrique reçoit le pire score possible, et le drapeau correspondant l'explique.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import pandas as pd

# Champs nécessaires au classement. Une valeur à qui il en manque plus de 40 % est écartée.
REQUIRED_FIELDS: tuple[str, ...] = (
    "income.FY0.TotalRevenue",
    "income.FY0.OperatingIncome",
    "income.FY0.EBITDA",
    "income.FY0.InterestExpense",
    "income.FY0.NetIncome",
    "income.FY0.NetIncomeCommonStockholders",
    "income.FY-3.TotalRevenue",
    "income.FY-3.OperatingIncome",
    "balance.FY0.TotalAssets",
    "balance.FY0.CurrentLiabilities",
    "balance.FY0.TotalDebt",
    "balance.FY0.CashAndCashEquivalents",
    "balance.FY0.OrdinarySharesNumber",
    "balance.FY-3.OrdinarySharesNumber",
    "cashflow.FY0.FreeCashFlow",
    "cashflow.FY-1.FreeCashFlow",
    "cashflow.FY-2.FreeCashFlow",
    "prices.current",
    "prices.history_12m",
)

METRIC_NAMES: tuple[str, ...] = (
    "rev_cagr_3y", "opinc_cagr_3y", "op_margin", "op_margin_trend_3y", "roce", "cash_conversion",
    "mom_12_1", "mom_6_1", "dist_sma200",
    "ev_ebit", "fcf_yield", "pe",
    "nd_ebitda", "interest_cov", "dilution_3y",
    "eps_rev_3m", "net_revisions",
)

FY_SPAN_MIN_DAYS = 1000          # trois exercices annuels, tolérance aux années de 52/53 semaines
FY_SPAN_MAX_DAYS = 1200
STALE_STATEMENT_DAYS = 500       # au-delà, l'état est signalé comme ancien
UNUSABLE_STATEMENT_DAYS = 730    # au-delà, la société ne publie plus : métriques manquantes
PRICE_MAX_GAP_DAYS = 10          # un cours plus vieux que dix jours ne vaut pas pour la date visée
SMA_WINDOW = 200
SMA_MIN_OBSERVATIONS = 150
LIQUIDITY_WINDOW_DAYS = 91       # trois mois calendaires
LIQUIDITY_OBSERVATIONS = 63      # séances attendues sur la période
CONSENSUS_MIN_ABS_EPS = 0.01     # en dessous, la variation relative n'a pas de sens

BEST = math.inf                  # sentinelle « cas le plus favorable » (couverture infinie)


@dataclass(frozen=True)
class FiscalYear:
    period_end: dt.date
    income: dict[str, float]
    balance: dict[str, float]
    cashflow: dict[str, float]
    currency: str | None


@dataclass
class StockMetrics:
    values: dict[str, float | None] = field(default_factory=dict)
    worst: set[str] = field(default_factory=set)      # métriques au pire score, pas manquantes
    missing_fields: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    # Éléments de contexte, repris dans le classement et le rapport.
    price: float | None = None
    price_date: dt.date | None = None
    market_cap_eur: float | None = None
    ev_eur: float | None = None
    traded_value_3m_eur: float | None = None
    fy0_period_end: dt.date | None = None
    statement_age_days: int | None = None
    statement_currency: str | None = None
    fx_rate_used: float | None = None
    fcf_negative_3y: bool | None = None
    net_debt: float | None = None
    ebitda: float | None = None
    consensus_incomplete: bool = True

    @property
    def n_missing(self) -> int:
        return len(self.missing_fields)

    @property
    def coverage_ratio(self) -> float:
        return 1.0 - len(self.missing_fields) / len(REQUIRED_FIELDS)

    @property
    def missing_metrics(self) -> list[str]:
        return [name for name in METRIC_NAMES if self.values.get(name) is None and name not in self.worst]


# ------------------------------------------------------------------------------ exercices


def select_fiscal_years(statements: pd.DataFrame) -> dict[int, FiscalYear]:
    """Range les exercices du plus récent au plus ancien : 0, -1, -2, -3."""
    if statements is None or statements.empty:
        return {}
    periods = sorted(statements["period_end"].unique(), reverse=True)
    years: dict[int, FiscalYear] = {}
    for rank, period_end in enumerate(periods[:4]):
        subset = statements[statements["period_end"] == period_end]
        currencies = [c for c in subset["currency"].dropna().unique()]
        years[-rank] = FiscalYear(
            period_end=period_end,
            income=_fields(subset, "income"),
            balance=_fields(subset, "balance"),
            cashflow=_fields(subset, "cashflow"),
            currency=currencies[0] if currencies else None,
        )
    return years


def year_three_years_before(years: dict[int, FiscalYear]) -> tuple[FiscalYear | None, int | None]:
    """Exercice clos environ trois ans avant le dernier, et l'écart réel en jours.

    On le cherche par sa date de clôture et non par son rang : Yahoo omet parfois un
    exercice, et comparer alors le dernier à l'avant-dernier donnerait une croissance
    calculée sur deux ans présentée comme trois.
    """
    fy0 = years.get(0)
    if fy0 is None:
        return None, None
    for rank in (-3, -2, -1):
        candidate = years.get(rank)
        if candidate is None:
            continue
        span = (fy0.period_end - candidate.period_end).days
        if FY_SPAN_MIN_DAYS <= span <= FY_SPAN_MAX_DAYS:
            return candidate, span
    return None, None


def _fields(subset: pd.DataFrame, statement: str) -> dict[str, float]:
    rows = subset[subset["statement"] == statement]
    return dict(zip(rows["field"], rows["value"], strict=True))


# ---------------------------------------------------------------------------------- cours


def total_return_index(prices: pd.DataFrame) -> pd.Series:
    """Indice de rendement total, dividendes réinvestis, calculé à partir des cours bruts.

    On n'utilise jamais le cours ajusté de Yahoo : il est recalculé rétroactivement, donc
    une série constituée jour après jour serait incohérente.
    """
    if prices is None or prices.empty:
        return pd.Series(dtype="float64")
    frame = prices.sort_values("date")
    close = frame["close"].astype("float64").to_numpy()
    dividend = frame["dividend"].fillna(0.0).astype("float64").to_numpy()
    index = [1.0]
    for i in range(1, len(close)):
        if close[i - 1] and close[i - 1] > 0:
            index.append(index[-1] * (close[i] + dividend[i]) / close[i - 1])
        else:
            index.append(index[-1])
    return pd.Series(index, index=list(frame["date"]), dtype="float64")


def value_on_or_before(series: pd.Series, day: dt.date, *, max_gap_days: int = PRICE_MAX_GAP_DAYS):
    """Dernière valeur connue à cette date, si elle n'est pas trop ancienne."""
    if series is None or series.empty:
        return None
    eligible = [d for d in series.index if d <= day]
    if not eligible:
        return None
    last = max(eligible)
    if (day - last).days > max_gap_days:
        return None
    return float(series.loc[last])


def momentum(index: pd.Series, as_of: dt.date, months_back: int) -> float | None:
    """Performance sur N mois hors dernier mois, comme le veut la littérature."""
    fin = value_on_or_before(index, _months_before(as_of, 1))
    debut = value_on_or_before(index, _months_before(as_of, months_back))
    if fin is None or debut is None or debut <= 0:
        return None
    return fin / debut - 1.0


def _months_before(day: dt.date, months: int) -> dt.date:
    return (pd.Timestamp(day) - pd.DateOffset(months=months)).date()


def sma200_distance(prices: pd.DataFrame, as_of: dt.date) -> float | None:
    """Écart du dernier cours à sa moyenne mobile 200 séances."""
    if prices is None or prices.empty:
        return None
    frame = prices[prices["date"] <= as_of].sort_values("date")
    recent = frame[frame["date"] >= as_of - dt.timedelta(days=300)]
    if len(recent) < SMA_MIN_OBSERVATIONS:
        return None
    closes = frame["close"].astype("float64").dropna()
    if closes.empty:
        return None
    fenetre = closes.tail(SMA_WINDOW)
    moyenne = float(fenetre.mean())
    dernier = float(closes.iloc[-1])
    if moyenne <= 0:
        return None
    return dernier / moyenne - 1.0


def median_traded_value(prices: pd.DataFrame, as_of: dt.date) -> float | None:
    """Médiane des capitaux échangés par séance sur trois mois, en devise de cotation.

    Les séances sans donnée comptent pour zéro : une valeur qui ne traite pas tous les
    jours est moins liquide, et l'ignorer surestimerait sa liquidité.
    """
    if prices is None or prices.empty:
        return None
    fenetre = prices[
        (prices["date"] <= as_of) & (prices["date"] > as_of - dt.timedelta(days=LIQUIDITY_WINDOW_DAYS))
    ]
    if fenetre.empty:
        return None
    echanges = (
        fenetre["close"].astype("float64") * fenetre["volume"].astype("float64")
    ).fillna(0.0).tolist()
    echanges += [0.0] * max(0, LIQUIDITY_OBSERVATIONS - len(echanges))
    return float(pd.Series(echanges).median())


def split_factor(splits: pd.DataFrame, start_excluded: dt.date, end_included: dt.date) -> float:
    """Produit des splits survenus dans l'intervalle : aligne deux nombres d'actions."""
    if splits is None or splits.empty:
        return 1.0
    fenetre = splits[(splits["date"] > start_excluded) & (splits["date"] <= end_included)]
    facteur = 1.0
    for ratio in fenetre["split_ratio"].astype("float64"):
        if ratio and ratio > 0:
            facteur *= ratio
    return facteur


# --------------------------------------------------------------------------- entrées dérivées


def _get(source: dict, *names: str) -> float | None:
    for name in names:
        value = source.get(name)
        if value is not None and not pd.isna(value):
            return float(value)
    return None


def derive_ebitda(fy: FiscalYear) -> tuple[float | None, bool]:
    value = _get(fy.income, "EBITDA", "NormalizedEBITDA")
    if value is not None:
        return value, False
    operating = _get(fy.income, "OperatingIncome")
    depreciation = _get(fy.income, "DepreciationAndAmortizationInIncomeStatement")
    if operating is not None and depreciation is not None:
        return operating + depreciation, True
    return None, False


def derive_fcf(fy: FiscalYear) -> tuple[float | None, bool]:
    value = _get(fy.cashflow, "FreeCashFlow")
    if value is not None:
        return value, False
    operating = _get(fy.cashflow, "OperatingCashFlow")
    capex = _get(fy.cashflow, "CapitalExpenditure")  # négatif chez Yahoo
    if operating is not None and capex is not None:
        return operating + capex, True
    return None, False


def derive_shares(fy: FiscalYear) -> tuple[float | None, bool]:
    value = _get(fy.balance, "OrdinarySharesNumber")
    if value is not None:
        return value, False
    issued = _get(fy.balance, "ShareIssued")
    treasury = _get(fy.balance, "TreasurySharesNumber")
    if issued is not None:
        return (issued - treasury) if treasury is not None else issued, True
    return None, False


def _cagr(fin: float | None, debut: float | None, span_days: int) -> float | None:
    if fin is None or debut is None or fin <= 0 or debut <= 0 or span_days <= 0:
        return None
    return (fin / debut) ** (365.25 / span_days) - 1.0


# ------------------------------------------------------------------- calcul d'une valeur


@dataclass(frozen=True)
class StockInputs:
    """Tout ce qu'il faut pour classer une valeur, tel que connu à la date de calcul."""

    ticker: str
    as_of: dt.date
    statements: pd.DataFrame          # états annuels disponibles à la date
    prices: pd.DataFrame              # cours jusqu'à la date
    splits: pd.DataFrame              # splits jusqu'à la date
    descriptor: dict                  # métadonnées (secteur, devises, nombre d'actions)
    consensus: dict | None            # consensus récent, ou None
    fx_rate: float | None             # 1 EUR = fx_rate (devise de publication) ; 1.0 si euro
    quote_fx_rate: float | None = 1.0 # 1 EUR = x devise de cotation ; None si inconnu


def compute_stock_metrics(inp: StockInputs) -> StockMetrics:
    """Calcule les dix-sept métriques d'une valeur. Aucune écriture, aucun effet de bord."""
    m = StockMetrics(values={name: None for name in METRIC_NAMES})
    years = select_fiscal_years(inp.statements)
    fy0 = years.get(0)
    missing: list[str] = []
    _consensus(m, inp.consensus)   # indépendant des états financiers

    # -- cours ---------------------------------------------------------------
    prices = inp.prices.sort_values("date") if inp.prices is not None and not inp.prices.empty else None
    index = total_return_index(prices) if prices is not None else pd.Series(dtype="float64")
    if prices is not None and not prices.empty:
        derniere = prices.iloc[-1]
        if (inp.as_of - derniere["date"]).days <= PRICE_MAX_GAP_DAYS:
            m.price = float(derniere["close"])
            m.price_date = derniere["date"]
    if m.price is None:
        missing.append("prices.current")

    m.values["mom_12_1"] = momentum(index, inp.as_of, 12)
    m.values["mom_6_1"] = momentum(index, inp.as_of, 6)
    m.values["dist_sma200"] = sma200_distance(prices, inp.as_of) if prices is not None else None
    if m.values["mom_12_1"] is None:
        missing.append("prices.history_12m")

    traded = median_traded_value(prices, inp.as_of) if prices is not None else None
    if traded is not None:
        if inp.quote_fx_rate and inp.quote_fx_rate > 0:
            traded = traded / inp.quote_fx_rate
        else:
            traded = None   # devise de cotation sans taux : la liquidité reste inconnue
            m.flags.append("devise_de_cotation_sans_taux")
    m.traded_value_3m_eur = traded

    # -- exercices -----------------------------------------------------------
    if fy0 is None:
        m.missing_fields = missing + [f for f in REQUIRED_FIELDS if not f.startswith("prices.")]
        m.flags.append("aucun_etat_financier")
        return m

    m.fy0_period_end = fy0.period_end
    m.statement_currency = fy0.currency
    m.statement_age_days = (inp.as_of - fy0.period_end).days
    m.fx_rate_used = inp.fx_rate

    if m.statement_age_days > UNUSABLE_STATEMENT_DAYS:
        # La société ne publie plus, ou Yahoo ne la suit plus : rien n'est exploitable.
        m.flags.append("etats_trop_anciens")
        m.missing_fields = missing + [f for f in REQUIRED_FIELDS if not f.startswith("prices.")]
        return m
    if m.statement_age_days > STALE_STATEMENT_DAYS:
        m.flags.append("etats_anciens")

    fy3, span_days = year_three_years_before(years)
    span_ok = fy3 is not None
    if not span_ok and len(years) > 1:
        m.flags.append("ecart_d_exercices_irregulier")

    # -- croissance et qualité ----------------------------------------------
    revenu0 = _get(fy0.income, "TotalRevenue", "OperatingRevenue")
    operating0 = _get(fy0.income, "OperatingIncome")
    revenu3 = _get(fy3.income, "TotalRevenue", "OperatingRevenue") if fy3 else None
    operating3 = _get(fy3.income, "OperatingIncome") if fy3 else None
    _track(missing, "income.FY0.TotalRevenue", revenu0)
    _track(missing, "income.FY0.OperatingIncome", operating0)
    _track(missing, "income.FY-3.TotalRevenue", revenu3)
    _track(missing, "income.FY-3.OperatingIncome", operating3)

    if span_ok:
        m.values["rev_cagr_3y"] = _cagr(revenu0, revenu3, span_days)
        m.values["opinc_cagr_3y"] = _cagr(operating0, operating3, span_days)

    marge0 = operating0 / revenu0 if (operating0 is not None and revenu0 and revenu0 > 0) else None
    marge3 = operating3 / revenu3 if (operating3 is not None and revenu3 and revenu3 > 0) else None
    m.values["op_margin"] = marge0
    if span_ok and marge0 is not None and marge3 is not None:
        m.values["op_margin_trend_3y"] = marge0 - marge3

    actifs = _get(fy0.balance, "TotalAssets")
    passif_courant = _get(fy0.balance, "CurrentLiabilities")
    _track(missing, "balance.FY0.TotalAssets", actifs)
    _track(missing, "balance.FY0.CurrentLiabilities", passif_courant)
    if operating0 is not None and actifs is not None and passif_courant is not None:
        capitaux = actifs - passif_courant
        m.values["roce"] = operating0 / capitaux if capitaux > 0 else None

    fcf0, derive0 = derive_fcf(fy0)
    fcf1, _ = derive_fcf(years[-1]) if -1 in years else (None, False)
    fcf2, _ = derive_fcf(years[-2]) if -2 in years else (None, False)
    _track(missing, "cashflow.FY0.FreeCashFlow", fcf0)
    _track(missing, "cashflow.FY-1.FreeCashFlow", fcf1)
    _track(missing, "cashflow.FY-2.FreeCashFlow", fcf2)
    if derive0:
        m.flags.append("fcf_reconstitue")
    if None not in (fcf0, fcf1, fcf2):
        m.fcf_negative_3y = fcf0 < 0 and fcf1 < 0 and fcf2 < 0

    resultat_net = _get(fy0.income, "NetIncome", "NetIncomeCommonStockholders")
    resultat_part_groupe = _get(fy0.income, "NetIncomeCommonStockholders", "NetIncome")
    _track(missing, "income.FY0.NetIncome", resultat_net)
    _track(missing, "income.FY0.NetIncomeCommonStockholders", resultat_part_groupe)
    if fcf0 is not None and resultat_net is not None and resultat_net > 0:
        m.values["cash_conversion"] = fcf0 / resultat_net

    # -- bilan ---------------------------------------------------------------
    ebitda, ebitda_derive = derive_ebitda(fy0)
    _track(missing, "income.FY0.EBITDA", ebitda)
    if ebitda_derive:
        m.flags.append("ebitda_reconstitue")
    m.ebitda = ebitda

    dette = _get(fy0.balance, "TotalDebt")
    tresorerie = _get(fy0.balance, "CashAndCashEquivalents", "CashCashEquivalentsAndShortTermInvestments")
    _track(missing, "balance.FY0.TotalDebt", dette)
    _track(missing, "balance.FY0.CashAndCashEquivalents", tresorerie)
    if dette is not None and tresorerie is not None:
        m.net_debt = dette - tresorerie
    elif dette is not None:
        m.net_debt = dette

    if m.net_debt is not None and ebitda is not None:
        if ebitda > 0:
            m.values["nd_ebitda"] = m.net_debt / ebitda
        elif m.net_debt <= 0:
            m.values["nd_ebitda"] = 0.0      # trésorerie nette : aucun levier
        else:
            m.worst.add("nd_ebitda")          # dette sans résultat pour la rembourser
            m.flags.append("ebitda_non_positif")

    charge_interets = _get(fy0.income, "InterestExpense")
    if charge_interets == 0:
        # Charge d'intérêt nulle déclarée : la couverture est infinie, c'est le meilleur cas.
        m.values["interest_cov"] = BEST
        m.flags.append("sans_charge_d_interet")
    elif charge_interets is None and m.net_debt is not None and m.net_debt <= 0:
        # Trésorerie nette et pas de charge publiée : la couverture est sans objet, on la
        # classe au mieux plutôt que de la déclarer manquante.
        m.values["interest_cov"] = BEST
        m.flags.append("sans_charge_d_interet")
    else:
        _track(missing, "income.FY0.InterestExpense", charge_interets)
        if operating0 is not None and charge_interets is not None:
            m.values["interest_cov"] = operating0 / abs(charge_interets)

    actions0, derive_actions = derive_shares(fy0)
    actions3, _ = derive_shares(fy3) if fy3 else (None, False)
    _track(missing, "balance.FY0.OrdinarySharesNumber", actions0)
    _track(missing, "balance.FY-3.OrdinarySharesNumber", actions3)
    if derive_actions:
        m.flags.append("actions_reconstituees")
    if span_ok and actions0 and actions3 and actions3 > 0:
        # Un split entre les deux exercices multiplie mécaniquement le nombre d'actions :
        # on l'annule, sinon toute société ayant divisé son nominal semblerait diluer.
        facteur = split_factor(inp.splits, fy3.period_end, fy0.period_end)
        m.values["dilution_3y"] = actions0 / (actions3 * facteur) - 1.0

    # -- valorisation --------------------------------------------------------
    _valorisation(m, inp, fy0, operating0, resultat_part_groupe, fcf0, actions0)

    m.missing_fields = missing
    return m


def _track(missing: list[str], name: str, value) -> None:
    if value is None:
        missing.append(name)


def _valorisation(m, inp, fy0, operating0, resultat, fcf0, actions0) -> None:
    """Multiples de valorisation : capitalisation en euros face à des comptes convertis."""
    if m.price is None or actions0 is None:
        return
    facteur = split_factor(inp.splits, fy0.period_end, m.price_date or inp.as_of)
    actions_now = actions0 * facteur
    if not inp.quote_fx_rate or inp.quote_fx_rate <= 0:
        return   # sans taux, la capitalisation en euros n'est pas calculable
    m.market_cap_eur = m.price * actions_now / inp.quote_fx_rate

    rate = inp.fx_rate
    if rate is None or rate <= 0:
        # Sans taux de change, on ne convertit rien : les trois multiples restent absents.
        m.flags.append("taux_de_change_absent" if fy0.currency != "EUR" else "devise_inconnue")
        return

    minoritaires = _get(fy0.balance, "MinorityInterest")
    if minoritaires is None:
        minoritaires = 0.0
        m.flags.append("interets_minoritaires_absents")
    if m.net_debt is not None:
        m.ev_eur = m.market_cap_eur + (m.net_debt + minoritaires) / rate

    if m.ev_eur is not None and operating0 is not None:
        if operating0 > 0:
            m.values["ev_ebit"] = m.ev_eur / (operating0 / rate)
        else:
            m.worst.add("ev_ebit")
            m.flags.append("resultat_operationnel_non_positif")

    if fcf0 is not None and m.market_cap_eur > 0:
        m.values["fcf_yield"] = (fcf0 / rate) / m.market_cap_eur

    if resultat is not None and m.market_cap_eur > 0:
        if resultat > 0:
            m.values["pe"] = m.market_cap_eur / (resultat / rate)
        else:
            m.worst.add("pe")
            m.flags.append("resultat_net_non_positif")


def _consensus(m, data: dict | None) -> None:
    """Variation du consensus sur trois mois et révisions nettes, horizons mis en commun."""
    if not data:
        m.consensus_incomplete = True
        return

    variations = []
    for horizon in ("0y", "1y"):
        courant = data.get(f"eps_{horizon}_current")
        ancien = data.get(f"eps_{horizon}_90d")
        if courant is None or ancien is None or abs(ancien) < CONSENSUS_MIN_ABS_EPS:
            continue
        variations.append((courant - ancien) / abs(ancien))
    if variations:
        m.values["eps_rev_3m"] = sum(variations) / len(variations)

    hausses = baisses = analystes = 0
    trouve = False
    for horizon in ("0y", "1y"):
        n = data.get(f"n_analysts_{horizon}")
        if not n:
            continue
        haut = data.get(f"up_30d_{horizon}")
        bas = data.get(f"down_30d_{horizon}")
        if haut is None and bas is None:
            continue
        hausses += haut or 0
        baisses += bas or 0
        analystes += n
        trouve = True
    if trouve and analystes > 0:
        m.values["net_revisions"] = (hausses - baisses) / analystes

    m.consensus_incomplete = (
        m.values.get("eps_rev_3m") is None or m.values.get("net_revisions") is None
    )
