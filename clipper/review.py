"""Serveur FastAPI pour l'interface de revue locale.

Sert les fichiers statiques et expose une API REST pour
valider, rejeter, ajuster les clips et modifier les titres.
"""

import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import Database

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent.parent / "static"


class AdjustRequest(BaseModel):
    start_time: float
    end_time: float


class TitleRequest(BaseModel):
    title: str


class ThumbnailRequest(BaseModel):
    thumbnail_path: str


def create_app(db: Database, storage_path: Path, cfg: dict) -> FastAPI:
    app = FastAPI(title="Clipper Review", docs_url=None, redoc_url=None)

    # --- Pages HTML ---

    @app.get("/")
    async def index():
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/review/{video_id}")
    async def review_page(video_id: str):
        return FileResponse(str(STATIC_DIR / "review.html"))

    # --- Fichiers médias (servis depuis data/) ---

    @app.get("/media/clips/{clip_id}.mp4")
    async def serve_clip(clip_id: str):
        path = storage_path / "clips" / f"{clip_id}.mp4"
        if not path.exists():
            raise HTTPException(404, "Clip non trouvé")
        return FileResponse(str(path), media_type="video/mp4")

    @app.get("/media/thumbnails/{clip_id}.jpg")
    async def serve_thumbnail(clip_id: str):
        path = storage_path / "thumbnails" / f"{clip_id}.jpg"
        if not path.exists():
            raise HTTPException(404, "Miniature non trouvée")
        return FileResponse(str(path), media_type="image/jpeg")

    # --- API REST ---

    @app.get("/api/videos")
    async def list_videos():
        videos = db.list_videos()
        result = []
        for v in videos:
            clips = db.list_clips(video_id=v["id"])
            result.append({
                **v,
                "clip_counts": {
                    "candidate": sum(1 for c in clips if c["status"] == "candidate"),
                    "validated": sum(1 for c in clips if c["status"] == "validated"),
                    "rejected": sum(1 for c in clips if c["status"] == "rejected"),
                    "cut": sum(1 for c in clips if c["status"] == "cut"),
                    "published": sum(1 for c in clips if c["status"] == "published"),
                    "total": len(clips),
                },
            })
        return result

    @app.get("/api/videos/{video_id}")
    async def get_video(video_id: str):
        video = db.get_video(video_id)
        if not video:
            raise HTTPException(404, "Vidéo non trouvée")
        return video

    @app.get("/api/videos/{video_id}/clips")
    async def list_clips(video_id: str):
        clips = db.list_clips(video_id=video_id)
        result = []
        for c in clips:
            clip = dict(c)
            # Désérialise les champs JSON
            for field in ("titles_json", "tags_json"):
                if clip.get(field):
                    try:
                        clip[field] = json.loads(clip[field])
                    except json.JSONDecodeError:
                        clip[field] = []
            result.append(clip)
        return result

    @app.post("/api/clips/{clip_id}/validate")
    async def validate_clip(clip_id: str):
        clip = db.get_clip(clip_id)
        if not clip:
            raise HTTPException(404, "Clip non trouvé")
        db.update_clip(clip_id, status="validated")
        logger.info("Clip %s validé", clip_id[:8])
        return {"status": "validated"}

    @app.post("/api/clips/{clip_id}/reject")
    async def reject_clip(clip_id: str):
        clip = db.get_clip(clip_id)
        if not clip:
            raise HTTPException(404, "Clip non trouvé")
        db.update_clip(clip_id, status="rejected")
        logger.info("Clip %s rejeté", clip_id[:8])
        return {"status": "rejected"}

    @app.post("/api/clips/{clip_id}/adjust")
    async def adjust_clip(clip_id: str, body: AdjustRequest):
        """Ajuste les bornes et remet le clip en candidate pour re-découpe."""
        clip = db.get_clip(clip_id)
        if not clip:
            raise HTTPException(404, "Clip non trouvé")

        new_duration = body.end_time - body.start_time
        sel_cfg = cfg.get("selection", {})
        min_dur = sel_cfg.get("min_duration", 120)
        max_dur = sel_cfg.get("max_duration", 300)

        if new_duration < min_dur:
            raise HTTPException(400, f"Durée {new_duration:.0f}s < minimum {min_dur}s")
        if new_duration > max_dur:
            raise HTTPException(400, f"Durée {new_duration:.0f}s > maximum {max_dur}s")

        # Supprimer l'ancien clip découpé si existant
        old_clip_path = clip.get("clip_path")
        if old_clip_path:
            p = Path(old_clip_path)
            if p.exists():
                p.unlink()

        db.update_clip(
            clip_id,
            start_time=body.start_time,
            end_time=body.end_time,
            duration=new_duration,
            clip_path=None,
            status="candidate",
        )
        logger.info(
            "Clip %s ajusté : [%.0fs-%.0fs]",
            clip_id[:8], body.start_time, body.end_time,
        )
        return {"status": "candidate", "start_time": body.start_time, "end_time": body.end_time}

    @app.post("/api/clips/{clip_id}/title")
    async def set_title(clip_id: str, body: TitleRequest):
        clip = db.get_clip(clip_id)
        if not clip:
            raise HTTPException(404, "Clip non trouvé")
        db.update_clip(clip_id, title_selected=body.title)
        return {"title_selected": body.title}

    @app.post("/api/clips/{clip_id}/thumbnail")
    async def set_thumbnail(clip_id: str, body: ThumbnailRequest):
        """Permet de remplacer la miniature par une image personnalisée."""
        clip = db.get_clip(clip_id)
        if not clip:
            raise HTTPException(404, "Clip non trouvé")
        if not Path(body.thumbnail_path).exists():
            raise HTTPException(400, "Fichier introuvable")
        db.update_clip(clip_id, thumbnail_path=body.thumbnail_path)
        return {"thumbnail_path": body.thumbnail_path}

    @app.get("/api/clips/{clip_id}")
    async def get_clip(clip_id: str):
        clip = db.get_clip(clip_id)
        if not clip:
            raise HTTPException(404, "Clip non trouvé")
        result = dict(clip)
        for field in ("titles_json", "tags_json"):
            if result.get(field):
                try:
                    result[field] = json.loads(result[field])
                except json.JSONDecodeError:
                    result[field] = []
        return result

    # Fichiers statiques (JS, CSS éventuels)
    if (STATIC_DIR / "assets").exists():
        app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")

    return app
