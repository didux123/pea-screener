"""Ligne de commande : universe, ingest, screen, report, research, status, serve."""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

from pea.config import ConfigError, load_config, load_env

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_RATE_LIMITED = 3
EXIT_DB_LOCKED = 4

log = logging.getLogger("pea")


def _parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"date attendue au format AAAA-MM-JJ, reçu « {value} »") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pea", description="Screener PEA : univers, données, score")
    parser.add_argument("--config", default="config.toml", help="chemin du fichier de configuration")
    parser.add_argument("-v", "--verbose", action="store_true", help="journal détaillé")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("universe", help="télécharge les listes de bourse et met à jour l'univers")
    p.add_argument("--no-download", action="store_true", help="réutilise les fichiers déjà archivés")
    p.add_argument("--no-resolve", action="store_true", help="n'associe pas de symbole Yahoo")

    p = sub.add_parser("ingest", help="cours, taux de change et fondamentaux")
    p.add_argument("--limit", type=int, help="ne traite que les N premières valeurs (mise au point)")

    p = sub.add_parser("screen", help="calcule et enregistre le classement")
    p.add_argument("--as-of", type=_parse_date, help="date de calcul (par défaut aujourd'hui)")

    p = sub.add_parser("report", help="regénère les fichiers d'un classement déjà calculé")
    p.add_argument("--as-of", type=_parse_date, help="date du classement à reprendre")

    sub.add_parser("status", help="état de la base : fraîcheur, couverture, valeurs dues")
    sub.add_parser("research", help="dossiers d'investissement (lot 2)")

    p = sub.add_parser("serve", help="sert les rapports en HTTP")
    p.add_argument("--port", type=int, default=8080)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration : {exc}", file=sys.stderr)
        return EXIT_USAGE
    load_env(cfg.root)

    try:
        return _dispatch(args, cfg)
    except KeyboardInterrupt:
        print("\nInterrompu.", file=sys.stderr)
        return EXIT_USAGE


def _dispatch(args, cfg) -> int:
    import duckdb

    from pea import db as db_module

    if args.command == "research":
        print("Le module research arrive au lot 2.")
        return EXIT_OK
    if args.command == "serve":
        return _serve(cfg, args.port)

    try:
        con = db_module.connect(cfg.db_path)
    except duckdb.IOException as exc:
        print(f"Base verrouillée par un autre processus : {exc}", file=sys.stderr)
        return EXIT_DB_LOCKED

    try:
        if args.command == "universe":
            return _universe(con, cfg, args)
        if args.command == "ingest":
            return _ingest(con, cfg, args)
        if args.command == "status":
            return _status(con)
        if args.command == "screen":
            return _screen(con, cfg, args)
        if args.command == "report":
            return _report(con, cfg, args)
        print(f"Commande inconnue : {args.command}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        con.close()


def _provider(cfg):
    from pea.data.yahoo import Pacer, YahooProvider

    return YahooProvider(cfg.cache_dir, Pacer(cfg.yahoo))


def _universe(con, cfg, args) -> int:
    from pea import universe as U

    provider = None if args.no_resolve else _provider(cfg)
    resume = U.refresh_universe(con, cfg, provider, download=not args.no_download)
    print(
        f"Univers au {resume.list_date} : {resume.n_isin} valeurs, {resume.n_listings} cotations "
        f"({', '.join(resume.sources)})"
    )
    print(
        f"  nouvelles {resume.n_new} | radiées {resume.n_delisted} | revenues {resume.n_returned} | "
        f"symboles résolus {resume.n_resolved} | sans symbole {resume.n_unresolved}"
    )
    if resume.n_unresolved:
        chemin = cfg.reports_dir / "universe_unresolved.csv"
        chemin.parent.mkdir(parents=True, exist_ok=True)
        U.unresolved_report(con).to_csv(chemin, index=False)
        print(f"  valeurs sans symbole listées dans {chemin}")
    return EXIT_OK


def _ingest(con, cfg, args) -> int:
    from pea.data.ingest import run_ingest

    resume = run_ingest(con, _provider(cfg), limit=args.limit)
    print(f"Cours        : {resume.prices.as_dict()}")
    print(f"Change       : {resume.fx.as_dict()}")
    print(f"Fondamentaux : {resume.fundamentals.as_dict()}")
    if resume.aborted:
        print(f"Interrompue : {resume.reason}", file=sys.stderr)
        return EXIT_RATE_LIMITED
    return EXIT_OK


def _screen(con, cfg, args) -> int:
    from pea import db as db_module
    from pea.screen.asof import load_pit
    from pea.screen.report import write_reports
    from pea.screen.runs import compare_with_previous, persist_run
    from pea.screen.score import run_screen

    as_of = args.as_of or dt.date.today()
    demarre = db_module.now_utc()
    pit = load_pit(con, as_of)
    if pit.universe.empty:
        print("Univers vide : lance d'abord `make universe` puis `make ingest`.", file=sys.stderr)
        return EXIT_USAGE

    resultat = run_screen(pit, overrides_path=cfg.overrides_path)
    run_id = persist_run(con, resultat, cfg, started_at=demarre)

    print(f"Classement {run_id} — régime {resultat.mode}")
    print(f"  {resultat.n_universe} valeurs examinées, {resultat.n_scored} classées, "
          f"{resultat.n_eliminated} écartées")
    if resultat.mode == "reconstructed":
        print("  Attention : date antérieure au démarrage du système, classement reconstitué.")
    print(f"  dernière information utilisée : {resultat.audit.max_info_date_used} | "
          f"dernière récupération : {resultat.audit.max_fetched_at_used}")

    retenues = resultat.scores[~resultat.scores["eliminated"]]
    if not retenues.empty:
        apercu = retenues.head(15)[["rank", "name", "sector", "total_score", "coverage_ratio"]]
        print()
        print(apercu.to_string(index=False, float_format=lambda v: f"{v:.1f}"))

    diff = compare_with_previous(con, run_id, as_of)
    if diff is not None:
        print()
        if diff.identical:
            print("  Identique au classement précédent de la même date.")
        else:
            print(f"  Écarts avec le classement précédent : {diff.n_score_changed} scores, "
                  f"{diff.n_rank_changed} rangs, {diff.n_added} entrées, {diff.n_removed} sorties")

    chemins = write_reports(con, run_id, cfg)
    print(f"\n  {chemins[-1]}")
    return EXIT_OK


def _report(con, cfg, args) -> int:
    from pea.screen.report import write_reports
    from pea.screen.runs import latest_run_id

    run_id = latest_run_id(con, args.as_of)
    if run_id is None:
        print("Aucun classement enregistré pour cette date.", file=sys.stderr)
        return EXIT_USAGE
    chemins = write_reports(con, run_id, cfg)
    for chemin in chemins:
        print(chemin)
    return EXIT_OK


def _status(con) -> int:
    from pea.data.ingest import due_tickers

    lignes = [
        ("valeurs actives", "SELECT count(*) FROM universe WHERE delisted_at IS NULL"),
        ("dont avec symbole", "SELECT count(*) FROM universe WHERE delisted_at IS NULL AND yf_ticker IS NOT NULL"),
        ("valeurs radiées", "SELECT count(*) FROM universe WHERE delisted_at IS NOT NULL"),
        ("valeurs avec cours", "SELECT count(DISTINCT ticker) FROM prices"),
        ("valeurs avec états", "SELECT count(DISTINCT ticker) FROM statements"),
        ("valeurs avec consensus", "SELECT count(DISTINCT ticker) FROM consensus"),
        ("dernier cours", "SELECT max(date) FROM prices"),
        ("devises suivies", "SELECT count(DISTINCT quote_ccy) FROM fx_rates"),
        ("classements enregistrés", "SELECT count(*) FROM runs"),
    ]
    for libelle, requete in lignes:
        print(f"{libelle:26s} {con.execute(requete).fetchone()[0]}")
    print(f"{'fondamentaux à rafraîchir':26s} {len(due_tickers(con, dt.date.today()))}")
    erreurs = con.execute(
        """
        SELECT status, count(*) FROM fetch_log
        WHERE status NOT IN ('ok', 'empty') GROUP BY status ORDER BY 2 DESC
        """
    ).fetchall()
    if erreurs:
        print("incidents :", ", ".join(f"{statut} {n}" for statut, n in erreurs))
    return EXIT_OK


def _serve(cfg, port: int) -> int:
    import functools
    import http.server
    import socketserver

    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(cfg.reports_dir))
    with socketserver.TCPServer(("", port), handler) as httpd:
        log.info("Rapports servis sur le port %d depuis %s", port, cfg.reports_dir)
        httpd.serve_forever()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
