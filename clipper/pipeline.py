"""Orchestration du pipeline pour une vidéo.

Chaque étape est idempotente : si elle a déjà été faite, elle est ignorée.
Une erreur dans une étape met la vidéo en status 'error' sans bloquer les autres.
"""

import logging
from pathlib import Path

from .db import Database
from .ingest import download_video
from .transcribe import transcribe_video
from .select import select_clips
from .bounds import recalibrate_clip
from .titles import generate_titles
from .cut import cut_clip
from .thumbnail import generate_thumbnail

logger = logging.getLogger(__name__)


def run_pipeline(video_id: str, db: Database, cfg: dict) -> None:
    """Exécute toutes les étapes du pipeline pour une vidéo."""
    video = db.get_video(video_id)
    if video is None:
        logger.error("Vidéo %s inconnue en base", video_id)
        return

    if video["status"] == "done":
        logger.info("Vidéo %s déjà traitée, ignorée", video_id)
        return

    storage_path = _resolve_storage(cfg)
    downloads_dir = storage_path / "downloads"
    clips_dir = storage_path / "clips"
    thumbnails_dir = storage_path / "thumbnails"

    try:
        # ---- Étape 1 : ingestion ----
        if video["status"] in ("pending", "downloading"):
            logger.info("[%s] Étape 1/6 : téléchargement", video_id)
            download_video(
                video_id=video_id,
                url=video["url"],
                downloads_dir=downloads_dir,
                db=db,
                video_format=cfg.get("video", {}).get(
                    "format",
                    "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
                ),
            )
            video = db.get_video(video_id)

        # ---- Étape 2 : transcription ----
        if video["status"] in ("downloaded", "transcribing"):
            if not video.get("audio_path"):
                raise RuntimeError("Chemin audio manquant après téléchargement")
            logger.info("[%s] Étape 2/6 : transcription", video_id)
            transcribe_video(
                video_id=video_id,
                audio_path=Path(video["audio_path"]),
                downloads_dir=downloads_dir,
                db=db,
                language=cfg.get("transcription", {}).get("language", "fr"),
                model=cfg.get("transcription", {}).get("model", "nova-2"),
            )
            video = db.get_video(video_id)

        # ---- Étape 3 : sélection des clips ----
        if video["status"] in ("transcribed", "selecting"):
            logger.info("[%s] Étape 3/6 : sélection des clips", video_id)
            clips = select_clips(video_id=video_id, db=db, cfg=cfg)
            logger.info("[%s] %d clips candidats sélectionnés", video_id, len(clips))
            video = db.get_video(video_id)

        # ---- Étapes 4-6 : bornes, titres, découpe, miniature ----
        # On traite tous les clips en status candidate ou cut (sans clip_path)
        candidates = [
            c for c in db.list_clips(video_id=video_id)
            if c["status"] in ("candidate",) or
               (c["status"] == "cut" and not c.get("clip_path"))
        ]

        if candidates:
            logger.info("[%s] Traitement de %d clips (bornes, titres, découpe, miniatures)…", video_id, len(candidates))
            _process_clips(candidates, video, db, cfg, downloads_dir, clips_dir, thumbnails_dir)

        db.update_video(video_id, status="selected")
        logger.info("[%s] Pipeline terminé", video_id)

    except Exception as exc:
        logger.exception("[%s] Erreur dans le pipeline : %s", video_id, exc)
        db.update_video(video_id, status="error", error_msg=str(exc))


def _process_clips(
    clips: list[dict],
    video: dict,
    db: Database,
    cfg: dict,
    downloads_dir: Path,
    clips_dir: Path,
    thumbnails_dir: Path,
) -> None:
    """Applique bornes + titres + découpe + miniature sur chaque clip candidat."""
    video_id = video["id"]

    for clip in clips:
        clip_id = clip["id"]
        try:
            # Étape 4 : recalage des bornes
            if video.get("transcript_path"):
                new_bounds = recalibrate_clip(
                    clip=clip,
                    transcript_path=Path(video["transcript_path"]),
                    cfg=cfg,
                )
                if new_bounds is None:
                    logger.warning("Clip %s rejeté après recalage des bornes", clip_id[:8])
                    db.update_clip(clip_id, status="rejected",
                                   reason=(clip.get("reason") or "") + " [rejeté : durée invalide après recalage]")
                    continue
                new_start, new_end = new_bounds
                if abs(new_start - clip["start_time"]) > 0.05 or abs(new_end - clip["end_time"]) > 0.05:
                    logger.info(
                        "Clip %s : bornes recalées [%.1f→%.1f] → [%.1f→%.1f]",
                        clip_id[:8], clip["start_time"], clip["end_time"], new_start, new_end,
                    )
                    db.update_clip(clip_id,
                                   start_time=new_start,
                                   end_time=new_end,
                                   duration=new_end - new_start)
                    clip = db.get_clip(clip_id)

            # Étape 5 : génération des titres
            if not clip.get("titles_json"):
                clip = generate_titles(clip=clip, video=video, db=db, cfg=cfg)

            # Étape 6 : découpe ffmpeg
            if not clip.get("clip_path"):
                video_path = Path(video["file_path"])
                cut_clip(clip=clip, video_path=video_path, clips_dir=clips_dir, db=db, cfg=cfg)
                clip = db.get_clip(clip_id)

            # Étape 7 : miniature
            if clip.get("clip_path") and not clip.get("thumbnail_path"):
                clip_path = Path(clip["clip_path"])
                generate_thumbnail(
                    clip=clip,
                    clip_path=clip_path,
                    thumbnails_dir=thumbnails_dir,
                    db=db,
                    cfg=cfg,
                )

        except Exception as exc:
            logger.error("Erreur sur le clip %s : %s", clip_id[:8], exc)
            # On continue avec les autres clips


def _resolve_storage(cfg: dict) -> Path:
    raw = cfg.get("storage_path", "data/")
    p = Path(raw)
    if not p.is_absolute():
        p = Path(__file__).parent.parent / p
    return p.resolve()
