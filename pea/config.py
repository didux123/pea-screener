"""Chargement de la configuration versionnée (config.toml) et des secrets (.env).

Une seule source de vérité : le fichier config.toml à la racine du dépôt. Les poids et
seuils du score composite ne sont volontairement pas configurables : ce sont des
constantes de pea/screen/score.py, versionnées par le SHA git enregistré dans chaque run.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config.toml")


class ConfigError(ValueError):
    """Configuration absente, mal formée ou hors bornes."""


@dataclass(frozen=True)
class YahooLimits:
    requests_per_pause: int
    pause_seconds: float
    min_interval_seconds: float


@dataclass(frozen=True)
class Costs:
    brokerage_fee_eur: float
    ftt_rate: float
    ftt_market_cap_threshold_eur: float


@dataclass(frozen=True)
class Config:
    root: Path
    data_dir: Path
    db_path: Path
    cache_dir: Path
    reports_dir: Path
    raw_universe_dir: Path
    overrides_path: Path
    euronext_mics: tuple[str, ...]
    yahoo: YahooLimits
    costs: Costs
    config_sha256: str


def _require(table: dict, section: str, key: str, kind: type):
    if key not in table:
        raise ConfigError(f"config.toml : clé absente [{section}].{key}")
    value = table[key]
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if not isinstance(value, kind) or isinstance(value, bool) != (kind is bool):
        raise ConfigError(f"config.toml : [{section}].{key} doit être de type {kind.__name__}")
    return value


def _positive(value: float, section: str, key: str, *, allow_zero: bool = False) -> float:
    if value < 0 or (value == 0 and not allow_zero):
        raise ConfigError(f"config.toml : [{section}].{key} doit être > 0")
    return value


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    """Lit config.toml, valide les bornes et calcule son empreinte."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config.toml introuvable : {path}")
    raw_bytes = path.read_bytes()
    try:
        raw = tomllib.loads(raw_bytes.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - message d'erreur
        raise ConfigError(f"config.toml illisible : {exc}") from exc

    root = path.resolve().parent
    data_dir = Path(_require(raw.get("paths", {}), "paths", "data_dir", str))
    if not data_dir.is_absolute():
        data_dir = root / data_dir

    yahoo_raw = raw.get("yahoo", {})
    yahoo = YahooLimits(
        requests_per_pause=int(
            _positive(_require(yahoo_raw, "yahoo", "requests_per_pause", int), "yahoo", "requests_per_pause")
        ),
        pause_seconds=_positive(
            _require(yahoo_raw, "yahoo", "pause_seconds", float), "yahoo", "pause_seconds", allow_zero=True
        ),
        min_interval_seconds=_positive(
            _require(yahoo_raw, "yahoo", "min_interval_seconds", float),
            "yahoo",
            "min_interval_seconds",
            allow_zero=True,
        ),
    )

    costs_raw = raw.get("costs", {})
    ftt_rate = _require(costs_raw, "costs", "ftt_rate", float)
    if not 0.0 <= ftt_rate <= 1.0:
        raise ConfigError("config.toml : [costs].ftt_rate doit être compris entre 0 et 1")
    costs = Costs(
        brokerage_fee_eur=_positive(
            _require(costs_raw, "costs", "brokerage_fee_eur", float),
            "costs",
            "brokerage_fee_eur",
            allow_zero=True,
        ),
        ftt_rate=ftt_rate,
        ftt_market_cap_threshold_eur=_positive(
            _require(costs_raw, "costs", "ftt_market_cap_threshold_eur", float),
            "costs",
            "ftt_market_cap_threshold_eur",
        ),
    )

    mics = raw.get("universe", {}).get("euronext_mics")
    if not isinstance(mics, list) or not mics or not all(isinstance(m, str) for m in mics):
        raise ConfigError("config.toml : [universe].euronext_mics doit être une liste non vide de MIC")

    return Config(
        root=root,
        data_dir=data_dir,
        db_path=data_dir / "pea.duckdb",
        cache_dir=data_dir / "cache",
        reports_dir=data_dir / "reports",
        raw_universe_dir=data_dir / "raw" / "universe",
        overrides_path=data_dir / "overrides" / "pea_eligibility.csv",
        euronext_mics=tuple(mics),
        yahoo=yahoo,
        costs=costs,
        config_sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )


def load_env(root: Path) -> None:
    """Charge le .env s'il existe. Aucun secret n'est requis au lot 1."""
    env_path = root / ".env"
    if not env_path.is_file():
        return
    from dotenv import load_dotenv

    load_dotenv(env_path, override=False)
