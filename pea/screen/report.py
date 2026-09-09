"""Production des fichiers de sortie : classement, valeurs écartées, couverture."""

from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from pea.screen.runs import load_run

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TOP_N = 100


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals["pct"] = _fmt_score
    env.globals["pourcent"] = _fmt_percent
    env.globals["ratio"] = _fmt_ratio
    return env


def _fmt_score(value) -> str:
    return "—" if value is None or pd.isna(value) else f"{float(value):.0f}"


def _fmt_percent(value) -> str:
    return "—" if value is None or pd.isna(value) else f"{float(value) * 100:.1f} %"


def _fmt_ratio(value) -> str:
    return "—" if value is None or pd.isna(value) else f"{float(value):.1f}"


def write_reports(con, run_id: str, cfg) -> list[Path]:
    """Écrit les fichiers d'un classement et les recopie dans « latest »."""
    run, scores, couverture = load_run(con, run_id)
    dossier = Path(cfg.reports_dir) / f"{run['as_of']:%Y-%m-%d}"
    dossier.mkdir(parents=True, exist_ok=True)

    retenues = scores[~scores["eliminated"].astype(bool)]
    ecartees = scores[scores["eliminated"].astype(bool)]

    chemins = []
    for nom, frame in (
        ("ranking.csv", scores),
        ("excluded.csv", ecartees),
        ("coverage.csv", couverture),
    ):
        chemin = dossier / nom
        frame.to_csv(chemin, index=False)
        chemins.append(chemin)

    raisons: dict[str, int] = {}
    for cellule in ecartees["elimination_reasons"].dropna():
        for raison in str(cellule).split(";"):
            if raison:
                raisons[raison] = raisons.get(raison, 0) + 1

    couverture_universe = couverture[couverture["population"] == "universe"].copy()
    total = (couverture_universe["n_present"] + couverture_universe["n_missing"]).sum()
    couverture_moyenne = (couverture_universe["n_present"].sum() / total) if total else 0.0
    # Les champs les moins renseignés en premier : c'est là que se joue la qualité du score.
    couverture_universe = couverture_universe.sort_values(["n_present", "field"])

    html = _environment().get_template("ranking.html.j2").render(
        run=run,
        top=retenues.head(TOP_N).to_dict("records"),
        raisons=sorted(raisons.items(), key=lambda item: -item[1]),
        couverture=couverture_universe.to_dict("records"),
        couverture_moyenne=float(couverture_moyenne),
        n_consensus_incomplet=int(retenues["consensus_incomplete"].fillna(True).astype(bool).sum()),
    )
    chemin_html = dossier / "ranking.html"
    chemin_html.write_text(html, encoding="utf-8")
    chemins.append(chemin_html)

    # Le fichier que consomme l'interface web (contrat décrit dans docs/ui/brief.md).
    from pea.screen.export import write_export

    chemins.append(write_export(con, run_id, cfg))

    dernier = Path(cfg.reports_dir) / "latest"
    dernier.mkdir(parents=True, exist_ok=True)
    for chemin in chemins:
        if chemin.parent != dernier:
            shutil.copy2(chemin, dernier / chemin.name)

    _copy_web(dossier)
    _copy_web(dernier)
    _write_index(Path(cfg.reports_dir))
    return chemins


def _copy_web(destination: Path) -> None:
    """Dépose les pages de l'interface à côté des données qu'elles lisent."""
    if not WEB_DIR.is_dir():
        return
    for fichier in WEB_DIR.iterdir():
        if fichier.is_file():
            shutil.copy2(fichier, destination / fichier.name)


def _write_index(reports_dir: Path) -> None:
    """Page d'accueil listant les classements disponibles."""
    dossiers = sorted(
        (p for p in reports_dir.iterdir() if p.is_dir() and p.name != "latest"),
        reverse=True,
    )
    lignes = "\n".join(
        f'<li><a href="{p.name}/index.html">{p.name}</a></li>' for p in dossiers
    )
    # La racine mène directement au dernier tableau de bord : une seule adresse à retenir.
    (reports_dir / "index.html").write_text(
        "<!doctype html><html lang=fr><head><meta charset=utf-8>"
        "<meta http-equiv=refresh content='0; url=latest/index.html'>"
        "<title>Screener PEA</title>"
        "<style>body{font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "max-width:40rem;margin:3rem auto;padding:0 1.5rem;background:#f5f2ec;color:#1c1c1a}"
        "@media(prefers-color-scheme:dark){body{background:#16161a;color:#e8e8e4}}"
        "a{color:inherit}h1{font-size:1.3rem}</style></head><body>"
        "<h1>Screener PEA</h1>"
        "<p><a href='latest/index.html'>Ouvrir le tableau de bord</a></p>"
        f"<p>Classements archivés :</p><ul>{lignes}</ul>"
        "</body></html>",
        encoding="utf-8",
    )
