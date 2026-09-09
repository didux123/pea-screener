"""Fige des réponses Yahoo dans tests/fixtures/yahoo pour les tests hors réseau.

À lancer à la main, rarement : `uv run python scripts/capture_fixtures.py`.
Le cache du fournisseur EST la fixture : les tests rejouent donc le code réel de bout en
bout, lecture de cache et normalisation comprises, sans jamais toucher au réseau.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pea.config import load_config  # noqa: E402
from pea.data.yahoo import Pacer, YahooProvider  # noqa: E402

CAPTURE_DATE = dt.date(2026, 9, 9)
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
PRICE_START = dt.date(2025, 1, 2)

# Chaque valeur illustre un cas que le code doit savoir traiter.
TICKERS = {
    "AI.PA": "grande capitalisation française, publie en euros",
    "VIRP.PA": "publication semestrielle : états trimestriels vides",
    "ASML.AS": "néerlandaise suivie par de nombreux analystes",
    "SBMO.AS": "publie ses comptes en dollars, cotée en euros",
    "SAP.DE": "allemande cotée à Xetra",
}
ISINS = {"FR0000120073": "Air Liquide, résolution par ISIN"}
# Réponses négatives : elles font partie des fixtures, un symbole inconnu doit être testé.
ABSENTS = {("MNEMO_FAUX.PA", "FR0000120073"), ("SYMBOLE_ABSENT.PA", "FR0000000000")}


def main() -> int:
    cfg = load_config(Path(__file__).resolve().parent.parent / "config.toml")
    provider = YahooProvider(FIXTURE_DIR, Pacer(cfg.yahoo), today=CAPTURE_DATE)
    print(f"Cache des fixtures : {FIXTURE_DIR / 'yahoo'}")
    for ticker, why in TICKERS.items():
        print(f"— {ticker} ({why})")
        for label, call in (
            ("descriptors", lambda t=ticker: provider.descriptors(t)),
            ("prices", lambda t=ticker: provider.prices(t, PRICE_START)),
            ("statements", lambda t=ticker: provider.statements(t)),
            ("consensus", lambda t=ticker: provider.consensus(t)),
            ("earnings_dates", lambda t=ticker: provider.earnings_dates(t)),
        ):
            try:
                result = call()
                size = len(result) if hasattr(result, "__len__") else "?"
                print(f"    {label:15s} {size}")
            except Exception as exc:  # noqa: BLE001 - script d'outillage
                print(f"    {label:15s} ÉCHEC {type(exc).__name__}: {exc}")
    for isin, why in ISINS.items():
        print(f"— {isin} ({why}) : {provider.resolve(isin, 'MNEMO_FAUX', 'XPAR')}")
    for symbole, isin in sorted(ABSENTS):
        mnemonique = symbole.rsplit(".", 1)[0]
        print(f"— absent {symbole} : {provider.resolve(isin, mnemonique, 'XPAR')}")
    print(f"\nFixtures écrites dans {FIXTURE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
