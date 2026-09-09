"""Cohérence des fichiers de déploiement : rien ne doit diverger en silence."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRONTAB = ROOT / "deploy" / "crontab"
ENTRYPOINT = ROOT / "deploy" / "entrypoint.sh"
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
MAKEFILE = ROOT / "Makefile"


def _lignes_de_taches() -> list[str]:
    return [
        ligne for ligne in CRONTAB.read_text(encoding="utf-8").splitlines()
        if ligne.strip() and not ligne.lstrip().startswith("#")
    ]


def test_crontab_bien_formee():
    taches = _lignes_de_taches()
    assert len(taches) == 2   # les jours de bourse, et le dimanche pour l'univers
    for ligne in taches:
        champs = ligne.split(maxsplit=5)
        assert len(champs) == 6, f"cinq champs horaires puis une commande : {ligne}"
        for champ in champs[:5]:
            assert re.fullmatch(r"[\d*/,\-]+", champ), f"champ horaire douteux : {champ}"


def test_les_taches_appellent_des_commandes_qui_existent():
    from pea.cli import build_parser

    connues = set(build_parser()._subparsers._group_actions[0].choices)
    appelees = set(re.findall(r"\bpea (\w+)", CRONTAB.read_text(encoding="utf-8")))
    assert appelees, "la crontab doit lancer des commandes pea"
    assert appelees <= connues, f"commandes inconnues : {appelees - connues}"


def test_l_ingestion_precede_le_classement():
    """DuckDB n'accepte qu'un écrivain : les deux commandes s'enchaînent, sans parallèle."""
    quotidienne = next(ligne for ligne in _lignes_de_taches() if "ingest" in ligne)
    assert quotidienne.index("ingest") < quotidienne.index("screen")
    assert "&&" in quotidienne


def test_point_d_entree_utilise_un_chemin_absolu():
    """supercronic se relance lui-même sans consulter le PATH."""
    contenu = ENTRYPOINT.read_text(encoding="utf-8")
    assert "exec /usr/local/bin/supercronic" in contenu
    assert "pea serve" in contenu


def test_image_epinglee_et_non_privilegiee():
    contenu = DOCKERFILE.read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in contenu
    assert "USER pea" in contenu                      # jamais root à l'exécution
    assert "sha1sum -c" in contenu                    # binaire téléchargé vérifié
    assert re.search(r"SUPERCRONIC_VERSION=v[\d.]+", contenu)
    assert "TZ=Europe/Paris" in contenu               # les horaires cron sont locaux


def test_compose_monte_les_donnees_et_ne_reclame_pas_de_secret():
    contenu = COMPOSE.read_text(encoding="utf-8")
    assert "./data:/app/data" in contenu
    assert "required: false" in contenu               # aucun secret nécessaire au lot 1
    assert "8080:8080" in contenu


@pytest.mark.parametrize(
    "cible", ["install", "universe", "ingest", "screen", "research", "report", "test"]
)
def test_cibles_du_makefile(cible):
    assert re.search(rf"^{cible}:", MAKEFILE.read_text(encoding="utf-8"), re.M)
