"""Construction et maintenance de la liste des valeurs éligibles au PEA.

Sources : la liste des actions d'Euronext (Paris, Amsterdam, Bruxelles, Lisbonne, y compris
Growth) et la liste des instruments négociables de Xetra. Les fichiers bruts sont archivés
sous data/raw/universe/ : ce sont eux qui portent la trace des radiations et protègent les
évaluations historiques du biais de survie.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import html
import io
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

from pea import db as db_module

log = logging.getLogger(__name__)

EURONEXT_URL = "https://live.euronext.com/en/pd_es/data/stocks"
EURONEXT_QUERY = {
    "display_datapoints": "dp_stocks",
    "display_filters": "df_stocks2",
    "display_table": "dt_stocks",
}
XETRA_DOWNLOADS_URL = (
    "https://www.cashmarket.deutsche-boerse.com/cash-en/trading/Tradable-Instruments-Xetra/Downloads"
)
XETRA_BASE_URL = "https://www.cashmarket.deutsche-boerse.com"
XETRA_CSV_PATTERN = re.compile(
    r"/resource/blob/\d+/[0-9a-f]+/data/t7-xetr-allTradableInstruments\.csv"
)
USER_AGENT = "pea-screener/0.1 (+usage personnel, non commercial)"
HTTP_TIMEOUT = 60

# UE 27 + Espace économique européen. Base de l'éligibilité PEA (siège social).
EU_EEA_ISO2 = frozenset(
    {
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
        "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
        "IS", "LI", "NO",
    }
)

# Noms de pays tels que Yahoo les écrit. Un pays absent de ce dictionnaire est signalé,
# jamais deviné : c'est la règle « aucune donnée imputée en silence ».
YAHOO_COUNTRY_TO_ISO2: dict[str, str] = {
    "Austria": "AT", "Belgium": "BE", "Bulgaria": "BG", "Croatia": "HR", "Cyprus": "CY",
    "Czech Republic": "CZ", "Czechia": "CZ", "Denmark": "DK", "Estonia": "EE", "Finland": "FI",
    "France": "FR", "Germany": "DE", "Greece": "GR", "Hungary": "HU", "Iceland": "IS",
    "Ireland": "IE", "Italy": "IT", "Latvia": "LV", "Liechtenstein": "LI", "Lithuania": "LT",
    "Luxembourg": "LU", "Malta": "MT", "Netherlands": "NL", "Norway": "NO", "Poland": "PL",
    "Portugal": "PT", "Romania": "RO", "Slovakia": "SK", "Slovenia": "SI", "Spain": "ES",
    "Sweden": "SE",
    # Régions ultrapériphériques et territoires : ils font partie de l'État membre, donc de
    # l'Union, et leurs sociétés sont éligibles au PEA.
    "Martinique": "FR", "Guadeloupe": "FR", "Réunion": "FR", "Reunion": "FR",
    "French Guiana": "FR", "Mayotte": "FR", "Saint Martin": "FR",
    "Azores": "PT", "Madeira": "PT", "Canary Islands": "ES", "Åland Islands": "FI",
    # Pays hors UE/EEE fréquents dans nos listes : mappés pour être écartés explicitement.
    "Switzerland": "CH", "United Kingdom": "GB", "United States": "US", "Canada": "CA",
    "Australia": "AU", "Bermuda": "BM", "Guernsey": "GG", "Jersey": "JE", "Isle of Man": "IM",
    "Cayman Islands": "KY", "British Virgin Islands": "VG", "Monaco": "MC", "Israel": "IL",
    "Japan": "JP", "China": "CN", "Hong Kong": "HK", "Singapore": "SG", "Brazil": "BR",
    "Mexico": "MX", "India": "IN", "South Africa": "ZA", "Turkey": "TR", "Russia": "RU",
    "Gibraltar": "GI", "Marshall Islands": "MH", "Panama": "PA", "Curacao": "CW",
    "Curaçao": "CW", "Netherlands Antilles": "AN", "Malaysia": "MY", "South Korea": "KR",
    "New Zealand": "NZ", "United Arab Emirates": "AE",
}

MIC_TO_YF_SUFFIX: dict[str, str] = {
    "XPAR": "PA", "ALXP": "PA", "XMLI": "PA",
    "XAMS": "AS", "ALXA": "AS",
    "XBRU": "BR", "ALXB": "BR", "MLXB": "BR",
    "XLIS": "LS", "ALXL": "LS", "ENXL": "LS",
    "XETR": "DE",
}

# Place de référence : d'abord celle du pays de l'ISIN, sinon l'ordre général ci-dessous.
COUNTRY_MIC_PREFERENCE: dict[str, tuple[str, ...]] = {
    "FR": ("XPAR", "ALXP", "XMLI"),
    "NL": ("XAMS", "ALXA"),
    "BE": ("XBRU", "ALXB", "MLXB"),
    "PT": ("XLIS", "ALXL", "ENXL"),
    "DE": ("XETR",),
}
MIC_PRECEDENCE: tuple[str, ...] = (
    "XPAR", "XAMS", "XBRU", "XLIS", "XETR", "ALXP", "ALXA", "ALXB", "ALXL", "XMLI", "MLXB", "ENXL",
)

LISTING_COLUMNS = ("isin", "mic", "mnemonic", "name", "quote_currency", "source")


@dataclass(frozen=True)
class UniverseSummary:
    list_date: dt.date
    n_listings: int
    n_isin: int
    n_new: int
    n_delisted: int
    n_returned: int
    n_resolved: int
    n_unresolved: int
    sources: tuple[str, ...]


def isin_is_valid(isin: str | None) -> bool:
    """Vérifie la forme et la clé de contrôle Luhn d'un ISIN."""
    if not isin or len(isin) != 12 or not isin[:2].isalpha() or not isin[-1].isdigit():
        return False
    if not isin[:2].isupper() or not isin[2:].isalnum():
        return False
    digits = "".join(str(int(c, 36)) if c.isalpha() else c for c in isin.upper())
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _strip_html(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", value or "")).strip()


# --------------------------------------------------------------------------- téléchargements


def download_euronext(dest_dir: Path, mics: tuple[str, ...], today: dt.date) -> Path:
    """Télécharge la liste des actions Euronext et l'archive en JSON brut."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"euronext_{today:%Y%m%d}.json"
    params = {"mics": ",".join(mics), **EURONEXT_QUERY}
    response = requests.post(
        EURONEXT_URL,
        params=params,
        data={"iDisplayLength": 5000, "iDisplayStart": 0, "sSortDir_0": "asc"},
        headers={"User-Agent": USER_AGENT},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("aaData") or []
    total = payload.get("iTotalRecords")
    if not rows:
        raise RuntimeError("Euronext : réponse vide, la liste n'a pas été téléchargée")
    if total is not None and len(rows) < total:
        raise RuntimeError(f"Euronext : {len(rows)} lignes reçues pour {total} annoncées")
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    log.info("Euronext : %d lignes archivées dans %s", len(rows), path)
    return path


def download_xetra(dest_dir: Path, today: dt.date) -> Path:
    """Télécharge la liste des instruments Xetra (le lien porte une empreinte changeante)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"xetra_{today:%Y%m%d}.csv"
    page = requests.get(
        XETRA_DOWNLOADS_URL, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT
    )
    page.raise_for_status()
    match = XETRA_CSV_PATTERN.search(page.text)
    if not match:
        raise RuntimeError("Xetra : lien vers t7-xetr-allTradableInstruments.csv introuvable")
    csv_response = requests.get(
        XETRA_BASE_URL + match.group(0), headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT
    )
    csv_response.raise_for_status()
    if "ISIN" not in csv_response.text[:4000]:
        raise RuntimeError("Xetra : le fichier téléchargé n'a pas l'en-tête attendu")
    path.write_bytes(csv_response.content)
    log.info("Xetra : fichier archivé dans %s", path)
    return path


# ------------------------------------------------------------------------------- analyseurs


def parse_euronext_file(path: Path) -> pd.DataFrame:
    """Extrait les cotations d'un fichier Euronext archivé.

    Une ligne du fichier peut porter plusieurs places (« XBRU, XPAR ») : elle donne alors
    une cotation par place.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    records: list[dict] = []
    for row in payload.get("aaData", []):
        if len(row) < 6:
            continue
        name = _strip_html(row[1])
        link = re.search(r"/product/equities/([A-Z0-9]+)-([A-Z]{4})", row[1] or "")
        isin = (row[2] or "").strip().upper()
        mnemonic = (row[3] or "").strip() or None
        mics = [m.strip().upper() for m in _strip_html(row[4]).split(",") if m.strip()]
        if link and link.group(2) not in mics:
            mics.insert(0, link.group(2))
        currency_cell = _strip_html(row[5]).split()
        currency = currency_cell[0] if currency_cell and currency_cell[0].isalpha() else None
        for mic in mics:
            records.append(
                {
                    "isin": isin,
                    "mic": mic,
                    "mnemonic": mnemonic,
                    "name": name,
                    "quote_currency": currency,
                    "source": "euronext",
                }
            )
    return pd.DataFrame(records, columns=list(LISTING_COLUMNS))


def parse_xetra_file(path: Path) -> pd.DataFrame:
    """Extrait les actions (`Instrument Type = CS`) d'un fichier Xetra archivé."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    header_index = next((i for i, line in enumerate(lines) if "ISIN" in line.split(";")), None)
    if header_index is None:
        raise ValueError(f"Xetra : en-tête introuvable dans {path}")
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:])), delimiter=";")
    records: list[dict] = []
    for row in reader:
        if (row.get("Instrument Type") or "").strip() != "CS":
            continue
        if (row.get("Instrument Status") or "Active").strip() != "Active":
            continue
        records.append(
            {
                "isin": (row.get("ISIN") or "").strip().upper(),
                "mic": (row.get("MIC Code") or "XETR").strip().upper(),
                "mnemonic": (row.get("Mnemonic") or "").strip() or None,
                "name": (row.get("Instrument") or "").strip(),
                "quote_currency": (row.get("Currency") or "").strip() or None,
                "source": "xetra",
            }
        )
    frame = pd.DataFrame(records, columns=list(LISTING_COLUMNS))
    return frame.drop_duplicates(subset=["isin", "mic"], keep="first")


def latest_files(raw_dir: Path) -> dict[str, tuple[Path, dt.date]]:
    """Dernier fichier archivé par source, avec la date lue dans son nom."""
    found: dict[str, tuple[Path, dt.date]] = {}
    raw_dir = Path(raw_dir)
    if not raw_dir.is_dir():
        return found
    for path in sorted(raw_dir.iterdir()):
        match = re.fullmatch(r"(euronext|xetra)_(\d{8})\.(json|csv)", path.name)
        if not match:
            continue
        source, stamp = match.group(1), match.group(2)
        file_date = dt.datetime.strptime(stamp, "%Y%m%d").date()
        if source not in found or file_date >= found[source][1]:
            found[source] = (path, file_date)
    return found


# ------------------------------------------------------------------- fusion et éligibilité


def merge_listings(
    frames: list[pd.DataFrame],
    *,
    allowed_mics: tuple[str, ...] | None = None,
    keep_isins: frozenset[str] | set[str] | None = None,
) -> pd.DataFrame:
    """Réunit les cotations et restreint l'univers.

    Ne sont conservées que les cotations dont l'ISIN a une clé de contrôle valide, dont la
    place fait partie du périmètre, et dont le pays de l'ISIN est dans l'UE/EEE. Ce dernier
    filtre définit l'univers PEA : sans lui, sept cents lignes américaines cotées à Xetra
    consommeraient des requêtes pour être écartées ensuite. Les ISIN listés dans le fichier
    d'exceptions (keep_isins) traversent ce filtre.
    """
    if not frames:
        return pd.DataFrame(columns=list(LISTING_COLUMNS))
    merged = pd.concat(frames, ignore_index=True)
    merged = merged[merged["isin"].map(isin_is_valid)]
    mics = set(allowed_mics) if allowed_mics is not None else set(MIC_TO_YF_SUFFIX)
    merged = merged[merged["mic"].isin(mics & set(MIC_TO_YF_SUFFIX))]
    keep = set(keep_isins or ())
    in_scope = merged["isin"].str[:2].isin(EU_EEA_ISO2) | merged["isin"].isin(keep)
    merged = merged[in_scope]
    merged = merged.drop_duplicates(subset=["isin", "mic"], keep="first").reset_index(drop=True)
    return merged


def choose_primary(listings: pd.DataFrame) -> pd.DataFrame:
    """Une ligne par ISIN : la place de référence, plus le suffixe Yahoo correspondant."""
    if listings.empty:
        return pd.DataFrame(
            columns=[*LISTING_COLUMNS, "isin_country", "yf_suffix", "candidate_ticker"]
        )

    def rank(row) -> tuple[int, int]:
        country = row["isin"][:2]
        preferred = COUNTRY_MIC_PREFERENCE.get(country, ())
        home = preferred.index(row["mic"]) if row["mic"] in preferred else len(preferred) + 50
        general = (
            MIC_PRECEDENCE.index(row["mic"]) if row["mic"] in MIC_PRECEDENCE else len(MIC_PRECEDENCE)
        )
        return (home, general)

    ordered = listings.copy()
    ordered["_rank"] = [rank(row) for _, row in ordered.iterrows()]
    ordered = ordered.sort_values(["isin", "_rank"], kind="stable")
    primary = ordered.drop_duplicates(subset=["isin"], keep="first").drop(columns=["_rank"])
    primary = primary.reset_index(drop=True)
    primary["isin_country"] = primary["isin"].str[:2]
    primary["yf_suffix"] = primary["mic"].map(MIC_TO_YF_SUFFIX)
    primary["candidate_ticker"] = [
        f"{row.mnemonic}.{row.yf_suffix}" if row.mnemonic and row.yf_suffix else None
        for row in primary.itertuples()
    ]
    return primary


def load_overrides(path: Path) -> dict[str, tuple[bool, str]]:
    """Décisions d'éligibilité prises à la main (données, pas configuration)."""
    path = Path(path)
    if not path.is_file():
        return {}
    overrides: dict[str, tuple[bool, str]] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            isin = (row.get("isin") or "").strip().upper()
            if not isin:
                continue
            flag = (row.get("pea_eligible") or "").strip().lower() in {"true", "1", "oui", "yes"}
            overrides[isin] = (flag, (row.get("reason") or "").strip())
    return overrides


def pea_eligibility(
    isin_country: str | None,
    yahoo_country: str | None,
    overrides: dict[str, tuple[bool, str]] | None = None,
    isin: str | None = None,
) -> tuple[bool | None, str]:
    """Éligibilité PEA : accord exigé entre le pays de l'ISIN et le pays Yahoo.

    Renvoie (True, ...) éligible, (False, ...) exclue, (None, ...) indéterminée : dans ce
    dernier cas la valeur est écartée du classement et listée dans le rapport de couverture.
    """
    overrides = overrides or {}
    if isin and isin in overrides:
        flag, reason = overrides[isin]
        return flag, f"override: {reason}" if reason else "override"

    # Les métadonnées viennent d'un tableau : une valeur absente arrive en NaN, pas en None.
    isin_country = isin_country if isinstance(isin_country, str) and isin_country.strip() else None
    yahoo_country = yahoo_country if isinstance(yahoo_country, str) and yahoo_country.strip() else None

    if not isin_country:
        return None, "pea_unknown: pays de l'ISIN absent"
    isin_ok = isin_country in EU_EEA_ISO2

    if yahoo_country is None:
        # Pas encore de métadonnées Yahoo : on tranche sur l'ISIN, confirmé au prochain passage.
        return (True, "isin_only") if isin_ok else (False, f"isin hors UE/EEE ({isin_country})")

    mapped = YAHOO_COUNTRY_TO_ISO2.get(yahoo_country.strip())
    if mapped is None:
        return None, f"pea_unknown: pays Yahoo non répertorié ({yahoo_country})"
    yahoo_ok = mapped in EU_EEA_ISO2

    if isin_ok and yahoo_ok:
        return True, "ok"
    if not isin_ok and not yahoo_ok:
        return False, f"hors UE/EEE (isin {isin_country}, yahoo {mapped})"
    return None, f"pea_conflict: isin {isin_country} vs yahoo {mapped}"


# --------------------------------------------------------------------------- rafraîchissement


def file_digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _record_file(con, source: str, path: Path, file_date: dt.date, n_rows: int) -> bool:
    """Enregistre un fichier source. Renvoie False s'il avait déjà été chargé à l'identique."""
    digest = file_digest(path)
    already = con.execute(
        "SELECT sha256 FROM universe_files WHERE source = ? AND file_date = ?", [source, file_date]
    ).fetchone()
    if already and already[0] == digest:
        return False
    con.execute(
        """
        INSERT INTO universe_files (source, file_date, path, sha256, n_rows, loaded_at_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, file_date) DO UPDATE SET
            path = EXCLUDED.path, sha256 = EXCLUDED.sha256,
            n_rows = EXCLUDED.n_rows, loaded_at_utc = EXCLUDED.loaded_at_utc
        """,
        [source, file_date, str(path), digest, n_rows, db_module.now_utc()],
    )
    return True


def refresh_universe(
    con,
    cfg,
    provider=None,
    *,
    today: dt.date | None = None,
    download: bool = True,
) -> UniverseSummary:
    """Recharge les listes de bourse, met à jour l'univers, résout les tickers manquants."""
    today = today or dt.date.today()
    raw_dir = cfg.raw_universe_dir
    if download:
        download_euronext(raw_dir, cfg.euronext_mics, today)
        download_xetra(raw_dir, today)

    files = latest_files(raw_dir)
    if not files:
        raise RuntimeError(f"Aucun fichier de bourse dans {raw_dir}. Lance `make universe`.")

    frames, list_date = [], None
    for source, (path, file_date) in sorted(files.items()):
        frame = parse_euronext_file(path) if source == "euronext" else parse_xetra_file(path)
        _record_file(con, source, path, file_date, len(frame))
        frames.append(frame)
        list_date = file_date if list_date is None else max(list_date, file_date)

    overrides = load_overrides(cfg.overrides_path)
    listings = merge_listings(
        frames,
        allowed_mics=(*cfg.euronext_mics, "XETR"),
        keep_isins=frozenset(isin for isin, (flag, _) in overrides.items() if flag),
    )
    primary = choose_primary(listings)
    now = db_module.now_utc()

    # Appartenance à cette liste : la base des radiations et des reruns.
    membership = listings[["isin", "mic", "source"]].copy()
    membership["list_date"] = list_date
    membership["fetched_at_utc"] = now
    db_module.insert_df(con, "universe_membership", membership, on_conflict="ignore")

    con.register("_listings", listings)
    con.execute(
        """
        INSERT INTO listings (isin, mic, mnemonic, name, quote_currency, source, first_seen, last_seen)
        SELECT isin, mic, mnemonic, name, quote_currency, source, ?, ? FROM _listings
        ON CONFLICT (isin, mic) DO UPDATE SET
            mnemonic = EXCLUDED.mnemonic, name = EXCLUDED.name,
            quote_currency = EXCLUDED.quote_currency, source = EXCLUDED.source,
            last_seen = EXCLUDED.last_seen, delisted_at = NULL
        """,
        [list_date, list_date],
    )
    con.execute(
        "UPDATE listings SET delisted_at = ? WHERE last_seen < ? AND delisted_at IS NULL",
        [list_date, list_date],
    )
    con.unregister("_listings")

    before = {
        row[0]: row[1]
        for row in con.execute("SELECT isin, delisted_at FROM universe").fetchall()
    }
    con.register("_primary", primary[[*LISTING_COLUMNS, "isin_country"]])
    con.execute(
        """
        INSERT INTO universe (
            isin, name, mic, mnemonic, quote_currency, isin_country,
            resolution, consecutive_not_found, first_seen, last_seen, fetched_at_utc)
        SELECT isin, name, mic, mnemonic, quote_currency, isin_country,
               'unresolved', 0, ?, ?, ?
        FROM _primary
        ON CONFLICT (isin) DO UPDATE SET
            name = EXCLUDED.name, mic = EXCLUDED.mic, mnemonic = EXCLUDED.mnemonic,
            quote_currency = EXCLUDED.quote_currency, last_seen = EXCLUDED.last_seen,
            delisted_at = NULL, fetched_at_utc = EXCLUDED.fetched_at_utc
        """,
        [list_date, list_date, now],
    )
    con.unregister("_primary")
    con.execute(
        "UPDATE universe SET delisted_at = ? WHERE last_seen < ? AND delisted_at IS NULL",
        [list_date, list_date],
    )

    n_new = sum(1 for isin in primary["isin"] if isin not in before)
    n_returned = sum(1 for isin in primary["isin"] if before.get(isin) is not None)
    n_delisted = con.execute(
        "SELECT count(*) FROM universe WHERE delisted_at = ?", [list_date]
    ).fetchone()[0]

    n_resolved = resolve_tickers(con, provider) if provider is not None else 0
    n_unresolved = con.execute(
        "SELECT count(*) FROM universe WHERE delisted_at IS NULL AND yf_ticker IS NULL"
    ).fetchone()[0]

    return UniverseSummary(
        list_date=list_date,
        n_listings=len(listings),
        n_isin=len(primary),
        n_new=n_new,
        n_delisted=n_delisted,
        n_returned=n_returned,
        n_resolved=n_resolved,
        n_unresolved=n_unresolved,
        sources=tuple(sorted(files)),
    )


def resolve_tickers(con, provider, *, limit: int | None = None) -> int:
    """Associe un symbole Yahoo aux valeurs qui n'en ont pas encore. Renvoie le nombre résolu."""
    rows = con.execute(
        """
        SELECT isin, mnemonic, mic FROM universe
        WHERE delisted_at IS NULL AND resolution IN ('unresolved', 'lost') AND yf_ticker IS NULL
        ORDER BY isin
        """
        + (f" LIMIT {int(limit)}" if limit else "")
    ).fetchall()
    resolved = 0
    for isin, mnemonic, mic in rows:
        result = provider.resolve(isin, mnemonic, mic)
        if result.ticker:
            con.execute(
                """
                UPDATE universe SET yf_ticker = ?, resolution = ?, resolved_at_utc = ?,
                                    consecutive_not_found = 0
                WHERE isin = ?
                """,
                [result.ticker, result.method, db_module.now_utc(), isin],
            )
            resolved += 1
            if result.descriptors:
                from pea.data.ingest import store_descriptors

                store_descriptors(con, result.ticker, result.descriptors)
        else:
            con.execute(
                "UPDATE universe SET resolution = 'unresolved', resolved_at_utc = ? WHERE isin = ?",
                [db_module.now_utc(), isin],
            )
    return resolved


def unresolved_report(con) -> pd.DataFrame:
    """Valeurs actives sans symbole Yahoo : elles doivent apparaître dans le rapport."""
    return con.execute(
        """
        SELECT isin, name, mic, mnemonic, isin_country, resolution
        FROM universe WHERE delisted_at IS NULL AND yf_ticker IS NULL
        ORDER BY isin
        """
    ).df()
