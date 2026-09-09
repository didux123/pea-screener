from __future__ import annotations

from pathlib import Path

import pytest

from pea.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parent.parent


def test_charge_la_config_du_depot():
    cfg = load_config(ROOT / "config.toml")
    assert cfg.costs.brokerage_fee_eur == 1.90
    assert cfg.costs.ftt_rate == 0.004
    assert cfg.costs.ftt_market_cap_threshold_eur == 1_000_000_000
    assert cfg.yahoo.requests_per_pause == 100
    assert "XPAR" in cfg.euronext_mics
    # Euronext Access est volontairement hors périmètre (décision documentée dans le README).
    assert "XMLI" not in cfg.euronext_mics
    assert cfg.db_path == cfg.data_dir / "pea.duckdb"
    assert cfg.data_dir.is_absolute()


def test_empreinte_stable_et_sensible_au_contenu(tmp_path):
    source = (ROOT / "config.toml").read_text()
    a = tmp_path / "a.toml"
    a.write_text(source)
    b = tmp_path / "b.toml"
    b.write_text(source.replace("brokerage_fee_eur = 1.90", "brokerage_fee_eur = 2.50"))
    assert load_config(a).config_sha256 == load_config(a).config_sha256
    assert load_config(a).config_sha256 != load_config(b).config_sha256


def test_fichier_absent():
    with pytest.raises(ConfigError):
        load_config("/inexistant/config.toml")


@pytest.mark.parametrize(
    "remplacement",
    [
        ("ftt_rate = 0.004", "ftt_rate = 1.5"),
        ("brokerage_fee_eur = 1.90", 'brokerage_fee_eur = "gratuit"'),
        ("requests_per_pause = 100", "requests_per_pause = 0"),
        ('euronext_mics = ["XPAR", "ALXP", "XAMS", "ALXA", "XBRU", "ALXB", "XLIS", "ALXL"]', "euronext_mics = []"),
    ],
)
def test_valeurs_hors_bornes_rejetees(tmp_path, remplacement):
    ancien, nouveau = remplacement
    source = (ROOT / "config.toml").read_text()
    assert ancien in source
    path = tmp_path / "config.toml"
    path.write_text(source.replace(ancien, nouveau))
    with pytest.raises(ConfigError):
        load_config(path)


def test_env_absent_ne_leve_pas(tmp_path):
    from pea.config import load_env

    load_env(tmp_path)  # ne doit rien faire, sans erreur
