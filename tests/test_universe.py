from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest

from pea import universe as U
from pea.config import load_config
from pea.data.provider import Resolution

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "universe"
ROOT = Path(__file__).resolve().parent.parent


class FakeProvider:
    """Résout un ticker comme le ferait Yahoo, sans réseau."""

    def __init__(self, connus: dict[str, str] | None = None, descripteurs=None):
        self.connus = connus or {}
        self.descripteurs = descripteurs or {}
        self.appels: list[tuple[str, str | None, str]] = []

    def resolve(self, isin, mnemonic, mic):
        self.appels.append((isin, mnemonic, mic))
        ticker = self.connus.get(isin)
        if ticker is None:
            return Resolution(ticker=None, method="unresolved", descriptors=None)
        return Resolution(
            ticker=ticker, method="mnemonic", descriptors=self.descripteurs.get(isin)
        )


@pytest.fixture
def cfg(tmp_path):
    source = (ROOT / "config.toml").read_text()
    (tmp_path / "config.toml").write_text(
        source.replace('data_dir = "data"', f'data_dir = "{tmp_path / "data"}"')
    )
    return load_config(tmp_path / "config.toml")


def _copie_fixtures(cfg, *, euronext=True, xetra=True, stamp="20260909"):
    cfg.raw_universe_dir.mkdir(parents=True, exist_ok=True)
    if euronext:
        (cfg.raw_universe_dir / f"euronext_{stamp}.json").write_text(
            (FIXTURES / "euronext_20260909.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
    if xetra:
        (cfg.raw_universe_dir / f"xetra_{stamp}.csv").write_text(
            (FIXTURES / "xetra_20260909.csv").read_text(encoding="utf-8"), encoding="utf-8"
        )


# ------------------------------------------------------------------------------------ ISIN


@pytest.mark.parametrize("isin", ["FR0000120073", "NL0000852564", "DE0005089031", "BE0974278104"])
def test_isin_valides(isin):
    assert U.isin_is_valid(isin)


@pytest.mark.parametrize("isin", ["FR0000053952", "FR000005395", "", None, "fr0000053951", "FR00000539@1"])
def test_isin_invalides(isin):
    assert not U.isin_is_valid(isin)


# -------------------------------------------------------------------------------- analyseurs


def test_parse_euronext():
    frame = U.parse_euronext_file(FIXTURES / "euronext_20260909.json")
    assert list(frame.columns) == list(U.LISTING_COLUMNS)
    air = frame[frame["isin"] == "FR0000120073"]
    assert air.iloc[0]["name"] == "AIR LIQUIDE"
    assert air.iloc[0]["mic"] == "XPAR"
    assert air.iloc[0]["quote_currency"] == "EUR"
    # Une ligne multi-place donne une cotation par place.
    abo = frame[frame["isin"] == "BE0974278104"]
    assert set(abo["mic"]) == {"XBRU", "XPAR"}
    # Une valeur sans cours (bon de souscription) n'a pas de devise imputée.
    bsa = frame[frame["isin"] == "FR001400ZRT0"]
    assert pd.isna(bsa.iloc[0]["quote_currency"])  # jamais imputée à EUR


def test_parse_xetra_ne_garde_que_les_actions():
    frame = U.parse_xetra_file(FIXTURES / "xetra_20260909.csv")
    assert list(frame.columns) == list(U.LISTING_COLUMNS)
    assert set(frame["mic"]) == {"XETR"}
    assert "DE0005089031" in set(frame["isin"])           # United Internet, action
    assert "BG9000011163" not in set(frame["isin"])       # ETF, écarté
    assert "CH0445689208" not in set(frame["isin"])       # ETN, écarté
    assert frame[frame["isin"] == "DE0005089031"].iloc[0]["source"] == "xetra"


def test_latest_files_prend_le_plus_recent(tmp_path):
    (tmp_path / "euronext_20260901.json").write_text("{}")
    (tmp_path / "euronext_20260908.json").write_text("{}")
    (tmp_path / "xetra_20260908.csv").write_text("x")
    (tmp_path / "note.txt").write_text("ignoré")
    found = U.latest_files(tmp_path)
    assert found["euronext"][1] == dt.date(2026, 9, 8)
    assert set(found) == {"euronext", "xetra"}


# --------------------------------------------------------------------- fusion / place de référence


def test_merge_ecarte_isin_invalides_places_et_pays_hors_scope():
    frame = pd.DataFrame(
        [
            {"isin": "FR0000120073", "mic": "XPAR", "mnemonic": "AI", "name": "AIR LIQUIDE",
             "quote_currency": "EUR", "source": "euronext"},
            {"isin": "FR0000053952", "mic": "XPAR", "mnemonic": "XX", "name": "clé fausse",
             "quote_currency": "EUR", "source": "euronext"},
            {"isin": "IT0003128367", "mic": "XMIL", "mnemonic": "ENEL", "name": "place hors périmètre",
             "quote_currency": "EUR", "source": "autre"},
            {"isin": "GG00B1RMC548", "mic": "XAMS", "mnemonic": "TFG", "name": "hors UE/EEE",
             "quote_currency": "USD", "source": "euronext"},
        ]
    )
    merged = U.merge_listings([frame])
    assert list(merged["isin"]) == ["FR0000120073"]


def test_place_de_reference_par_pays_de_isin():
    frames = [
        U.parse_euronext_file(FIXTURES / "euronext_20260909.json"),
        U.parse_xetra_file(FIXTURES / "xetra_20260909.csv"),
    ]
    primary = U.choose_primary(U.merge_listings(frames))
    par_isin = primary.set_index("isin")
    # Nexans est cotée à Paris (Euronext) et sur Xetra : la place de référence est Paris.
    assert par_isin.loc["FR0000044448", "mic"] == "XPAR"
    assert par_isin.loc["FR0000044448", "candidate_ticker"].endswith(".PA")
    # Crédit Agricole n'est présente que dans la liste Xetra de la fixture.
    assert par_isin.loc["FR0000045072", "mic"] == "XETR"
    # EURONEXT NV, ISIN néerlandais coté sur quatre places -> Amsterdam.
    assert par_isin.loc["NL0006294274", "mic"] == "XAMS"
    # ABO Group, ISIN belge coté Bruxelles et Paris -> Bruxelles.
    assert par_isin.loc["BE0974278104", "mic"] == "XBRU"
    assert par_isin.loc["BE0974278104", "candidate_ticker"].endswith(".BR")
    # Valeur Growth : la place de référence est bien Growth Paris.
    assert par_isin.loc["FR0013341781", "mic"] == "ALXP"
    assert par_isin.loc["FR0013341781", "candidate_ticker"] == "AL2SI.PA"
    assert par_isin.loc["DE0005089031", "isin_country"] == "DE"
    # Un ISIN, une seule ligne.
    assert primary["isin"].is_unique


def test_merge_restreint_aux_places_du_perimetre():
    frame = pd.DataFrame(
        [
            {"isin": "FR0013341781", "mic": "ALXP", "mnemonic": "AL2SI", "name": "2CRSI",
             "quote_currency": "EUR", "source": "euronext"},
            {"isin": "FR0014004QR6", "mic": "XMLI", "mnemonic": "MLXXX", "name": "Euronext Access",
             "quote_currency": "EUR", "source": "euronext"},
        ]
    )
    merged = U.merge_listings([frame], allowed_mics=("XPAR", "ALXP", "XETR"))
    assert list(merged["mic"]) == ["ALXP"]  # Access est hors périmètre (décision documentée)


def test_merge_garde_les_isin_du_fichier_d_exceptions():
    frame = pd.DataFrame(
        [
            {"isin": "GG00B1RMC548", "mic": "XAMS", "mnemonic": "TFG", "name": "hors UE/EEE",
             "quote_currency": "USD", "source": "euronext"},
        ]
    )
    assert U.merge_listings([frame]).empty
    garde = U.merge_listings([frame], keep_isins={"GG00B1RMC548"})
    assert list(garde["isin"]) == ["GG00B1RMC548"]


def test_double_cotation_paris_et_xetra_prefere_paris():
    frame = pd.DataFrame(
        [
            {"isin": "FR0000121014", "mic": "XETR", "mnemonic": "MOH", "name": "LVMH",
             "quote_currency": "EUR", "source": "xetra"},
            {"isin": "FR0000121014", "mic": "XPAR", "mnemonic": "MC", "name": "LVMH",
             "quote_currency": "EUR", "source": "euronext"},
        ]
    )
    primary = U.choose_primary(U.merge_listings([frame]))
    assert len(primary) == 1
    assert primary.iloc[0]["mic"] == "XPAR"
    assert primary.iloc[0]["candidate_ticker"] == "MC.PA"


# -------------------------------------------------------------------------------- éligibilité


def test_eligibilite_accord():
    assert U.pea_eligibility("FR", "France")[0] is True
    assert U.pea_eligibility("NO", "Norway")[0] is True


def test_eligibilite_hors_ue():
    ok, raison = U.pea_eligibility("GB", "United Kingdom")
    assert ok is False and "hors UE/EEE" in raison


def test_eligibilite_conflit_puis_override():
    ok, raison = U.pea_eligibility("NL", "Switzerland", isin="NL0000226223")
    assert ok is None and "pea_conflict" in raison
    overrides = U.load_overrides(ROOT / "data" / "overrides" / "pea_eligibility.csv")
    ok, raison = U.pea_eligibility("NL", "Switzerland", overrides, isin="NL0000226223")
    assert ok is True and raison.startswith("override")


def test_territoires_francais_sont_dans_l_union():
    """La Martinique est un département français : ses sociétés sont éligibles au PEA."""
    assert U.pea_eligibility("FR", "Martinique")[0] is True
    assert U.pea_eligibility("PT", "Madeira")[0] is True


def test_eligibilite_pays_yahoo_inconnu():
    ok, raison = U.pea_eligibility("FR", "Wakanda")
    assert ok is None and "pea_unknown" in raison


def test_eligibilite_sans_metadonnees_yahoo():
    assert U.pea_eligibility("FR", None) == (True, "isin_only")
    assert U.pea_eligibility("US", None)[0] is False


# ---------------------------------------------------------------------------- rafraîchissement


def test_refresh_construit_univers_et_membership(con, cfg):
    _copie_fixtures(cfg)
    provider = FakeProvider({"FR0000120073": "AI.PA"})
    resume = U.refresh_universe(con, cfg, provider, today=dt.date(2026, 9, 9), download=False)

    assert resume.list_date == dt.date(2026, 9, 9)
    assert resume.n_isin > 20
    assert resume.n_new == resume.n_isin
    assert resume.n_resolved == 1
    assert resume.sources == ("euronext", "xetra")

    n_membership = con.execute("SELECT count(*) FROM universe_membership").fetchone()[0]
    assert n_membership == resume.n_listings
    assert con.execute(
        "SELECT yf_ticker, resolution FROM universe WHERE isin = 'FR0000120073'"
    ).fetchone() == ("AI.PA", "mnemonic")
    # Les ISIN hors UE/EEE ne rentrent pas dans l'univers : ils ne sont pas éligibles au PEA
    # et leur résolution coûterait des requêtes pour rien.
    pays = {row[0] for row in con.execute("SELECT DISTINCT isin_country FROM universe").fetchall()}
    assert pays <= U.EU_EEA_ISO2
    assert "CH" not in pays and "GG" not in pays


def test_meme_fichier_deux_fois_ne_duplique_pas(con, cfg):
    _copie_fixtures(cfg)
    provider = FakeProvider()
    a = U.refresh_universe(con, cfg, provider, today=dt.date(2026, 9, 9), download=False)
    b = U.refresh_universe(con, cfg, provider, today=dt.date(2026, 9, 9), download=False)
    assert a.n_isin == b.n_isin
    assert con.execute("SELECT count(*) FROM universe_files").fetchone()[0] == 2  # euronext + xetra
    assert con.execute("SELECT count(*) FROM universe").fetchone()[0] == a.n_isin
    assert b.n_new == 0


def test_radiation_puis_retour(con, cfg):
    _copie_fixtures(cfg)
    provider = FakeProvider()
    U.refresh_universe(con, cfg, provider, today=dt.date(2026, 9, 9), download=False)

    # Liste suivante sans Air Liquide : la valeur est conservée, avec sa date de sortie.
    payload = json.loads((FIXTURES / "euronext_20260909.json").read_text(encoding="utf-8"))
    payload["aaData"] = [r for r in payload["aaData"] if r[2] != "FR0000120073"]
    (cfg.raw_universe_dir / "euronext_20260916.json").write_text(json.dumps(payload), encoding="utf-8")
    (cfg.raw_universe_dir / "xetra_20260916.csv").write_text(
        (FIXTURES / "xetra_20260909.csv").read_text(encoding="utf-8"), encoding="utf-8"
    )
    U.refresh_universe(con, cfg, provider, today=dt.date(2026, 9, 16), download=False)

    row = con.execute(
        "SELECT delisted_at, last_seen FROM universe WHERE isin = 'FR0000120073'"
    ).fetchone()
    assert row[0] == dt.date(2026, 9, 16)
    assert row[1] == dt.date(2026, 9, 9)

    # Puis elle revient : la date de sortie est effacée, la valeur n'a jamais été supprimée.
    (cfg.raw_universe_dir / "euronext_20260923.json").write_text(
        (FIXTURES / "euronext_20260909.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (cfg.raw_universe_dir / "xetra_20260923.csv").write_text(
        (FIXTURES / "xetra_20260909.csv").read_text(encoding="utf-8"), encoding="utf-8"
    )
    resume = U.refresh_universe(con, cfg, provider, today=dt.date(2026, 9, 23), download=False)
    assert con.execute(
        "SELECT delisted_at FROM universe WHERE isin = 'FR0000120073'"
    ).fetchone()[0] is None
    assert resume.n_returned >= 1


def test_resolution_appelle_seulement_les_non_resolus(con, cfg):
    _copie_fixtures(cfg)
    provider = FakeProvider({"FR0000120073": "AI.PA"})
    U.refresh_universe(con, cfg, provider, today=dt.date(2026, 9, 9), download=False)
    premier_tour = len(provider.appels)
    provider.appels.clear()
    U.refresh_universe(con, cfg, provider, today=dt.date(2026, 9, 9), download=False)
    assert premier_tour > 1
    assert all(isin != "FR0000120073" for isin, _, _ in provider.appels)


def test_rapport_des_non_resolus(con, cfg):
    _copie_fixtures(cfg)
    U.refresh_universe(
        con, cfg, FakeProvider({"FR0000120073": "AI.PA"}), today=dt.date(2026, 9, 9), download=False
    )
    rapport = U.unresolved_report(con)
    assert "FR0000120073" not in set(rapport["isin"])
    assert len(rapport) > 0
