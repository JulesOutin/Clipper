"""Point d'entrée CLI — commandes clipper add / run / status / review / publish / stats."""

import argparse
import logging
import sys
from pathlib import Path

from tabulate import tabulate

from .config import load_config, get_storage_path
from .db import open_db
from .ingest import resolve_urls, extract_video_id, fetch_metadata_only
from .cost import estimate_cost, get_video_cost_summary, get_total_cost
from .pipeline import run_pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def cmd_add(args, cfg, db):
    """Ajoute une URL (vidéo, playlist, chaîne) et lance le pipeline."""
    logger.info("Résolution de l'URL : %s", args.url)
    try:
        urls = resolve_urls(args.url)
    except Exception as exc:
        logger.error("Impossible de résoudre l'URL : %s", exc)
        sys.exit(1)

    added = 0
    skipped = 0
    for url in urls:
        video_id = extract_video_id(url)
        if not video_id:
            logger.warning("ID non extrait pour %s, ignoré", url)
            continue

        existing = db.get_video(video_id)
        if existing:
            logger.info("Vidéo %s déjà connue (status: %s), ignorée", video_id, existing["status"])
            skipped += 1
            continue

        # Récupération des métadonnées sans télécharger
        try:
            meta = fetch_metadata_only(url)
        except Exception:
            meta = {}

        db.upsert_video(
            video_id,
            url=url,
            title=meta.get("title"),
            channel=meta.get("uploader") or meta.get("channel"),
            duration=meta.get("duration"),
        )
        added += 1

    logger.info(
        "%d vidéo(s) ajoutée(s), %d ignorée(s) (déjà connues)", added, skipped
    )

    if added > 0 and not args.no_run:
        logger.info("Lancement du pipeline…")
        _run_pending(db, cfg, video_id_filter=None)


def cmd_run(args, cfg, db):
    """Lance ou reprend le pipeline pour les vidéos en attente."""
    video_id_filter = getattr(args, "video_id", None)
    _run_pending(db, cfg, video_id_filter=video_id_filter)


def _run_pending(db, cfg, video_id_filter: str | None):
    if video_id_filter:
        video = db.get_video(video_id_filter)
        if video is None:
            logger.error("Vidéo %s inconnue", video_id_filter)
            return
        pending = [video]
    else:
        pending = db.list_pending_videos()

    if not pending:
        logger.info("Aucune vidéo en attente de traitement")
        return

    logger.info("%d vidéo(s) à traiter", len(pending))
    for video in pending:
        logger.info("--- Traitement de : %s ---", video.get("title") or video["id"])
        run_pipeline(video["id"], db, cfg)


def cmd_status(args, cfg, db):
    """Affiche l'état du pipeline et les coûts."""
    videos = db.list_videos()
    if not videos:
        print("Aucune vidéo en base.")
        return

    sel_model = cfg.get("selection", {}).get("model", "claude-sonnet-4-6")

    rows = []
    for v in videos:
        clips = db.list_clips(video_id=v["id"])
        n_candidates = sum(1 for c in clips if c["status"] in ("candidate", "cut"))
        n_validated = sum(1 for c in clips if c["status"] == "validated")
        n_published = sum(1 for c in clips if c["status"] == "published")

        real_cost = get_video_cost_summary(v["id"], db)
        duration = v.get("duration") or 0
        estimated = estimate_cost(duration, model=sel_model)

        cost_str = f"${real_cost['total']:.3f}" if real_cost["total"] else f"~${estimated['total']:.3f} est."

        rows.append([
            v["id"][:11],
            (v.get("title") or "—")[:40],
            v["status"],
            f"{n_candidates}/{n_validated}/{n_published}",
            f"{duration//60:.0f}min",
            cost_str,
        ])

    print(tabulate(
        rows,
        headers=["ID", "Titre", "Statut", "Cand/Val/Pub", "Durée", "Coût"],
        tablefmt="rounded_outline",
    ))

    total = get_total_cost(db)
    print(f"\nCoût total cumulé : ${total:.4f}")


def cmd_review(args, cfg, db):
    """Lance le serveur de revue FastAPI."""
    try:
        import uvicorn
        from .review import create_app
    except ImportError as exc:
        logger.error("Dépendances manquantes pour la revue : %s", exc)
        sys.exit(1)

    storage_path = get_storage_path(cfg)
    app = create_app(db, storage_path, cfg)
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8000)
    logger.info("Interface de revue disponible sur http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")


def cmd_publish(args, cfg, db):
    """Publie les clips validés dans la file YouTube."""
    try:
        from .publish import publish_validated_clips
    except ImportError as exc:
        logger.error("Module de publication non disponible : %s", exc)
        sys.exit(1)
    publish_validated_clips(db, cfg)


def cmd_stats(args, cfg, db):
    """Télécharge les stats YouTube et exporte en CSV."""
    try:
        from .stats import fetch_and_store_stats, export_csv
    except ImportError as exc:
        logger.error("Module de stats non disponible : %s", exc)
        sys.exit(1)
    fetch_and_store_stats(db, cfg)
    output = Path("clipper_stats.csv")
    export_csv(db, output)
    logger.info("Stats exportées dans %s", output)


def main():
    cfg = load_config()
    storage_path = get_storage_path(cfg)
    db = open_db(storage_path)

    parser = argparse.ArgumentParser(
        prog="clipper",
        description="Transforme des vidéos YouTube longues en clips courts",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- add ---
    p_add = sub.add_parser("add", help="Ajoute une URL et lance le pipeline")
    p_add.add_argument("url", help="URL YouTube (vidéo, playlist ou chaîne)")
    p_add.add_argument(
        "--no-run", action="store_true",
        help="Ajoute sans lancer le pipeline immédiatement",
    )

    # --- run ---
    p_run = sub.add_parser("run", help="(Re)lance le pipeline pour les vidéos en attente")
    p_run.add_argument("--video-id", help="Traiter uniquement cette vidéo")

    # --- status ---
    sub.add_parser("status", help="Affiche l'état et les coûts")

    # --- review ---
    p_review = sub.add_parser("review", help="Lance l'interface de revue locale")
    p_review.add_argument("--host", default="127.0.0.1")
    p_review.add_argument("--port", type=int, default=8000)

    # --- publish ---
    sub.add_parser("publish", help="Publie les clips validés sur YouTube")

    # --- stats ---
    sub.add_parser("stats", help="Récupère les stats YouTube et exporte en CSV")

    args = parser.parse_args()

    dispatch = {
        "add": cmd_add,
        "run": cmd_run,
        "status": cmd_status,
        "review": cmd_review,
        "publish": cmd_publish,
        "stats": cmd_stats,
    }

    try:
        dispatch[args.command](args, cfg, db)
    except KeyboardInterrupt:
        logger.info("Interruption utilisateur")
    finally:
        db.close()


if __name__ == "__main__":
    main()
