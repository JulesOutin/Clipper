"""Ingestion des vidéos YouTube via yt-dlp.

Télécharge la vidéo et extrait l'audio séparément pour Deepgram.
Idempotent : ne re-télécharge pas si les fichiers existent déjà.
"""

import json
import logging
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp

from .db import Database

logger = logging.getLogger(__name__)


def extract_video_id(url: str) -> str | None:
    """Extrait l'ID YouTube depuis une URL vidéo classique."""
    parsed = urlparse(url)
    if parsed.hostname in ("youtu.be",):
        return parsed.path.lstrip("/")
    if parsed.hostname in ("www.youtube.com", "youtube.com", "m.youtube.com"):
        qs = parse_qs(parsed.query)
        return qs.get("v", [None])[0]
    return None


def resolve_urls(url: str) -> list[str]:
    """
    Résout une URL YouTube (vidéo simple, playlist, chaîne) en liste d'URLs vidéo.
    Utilise --flat-playlist pour éviter de tout télécharger d'un coup.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info is None:
        raise ValueError(f"Impossible de résoudre l'URL : {url}")

    # Playlist ou chaîne : itère sur les entrées
    if info.get("_type") in ("playlist", "channel"):
        entries = info.get("entries", [])
        urls = []
        for entry in entries:
            if entry and entry.get("id"):
                urls.append(f"https://www.youtube.com/watch?v={entry['id']}")
        logger.info("%d vidéo(s) trouvée(s) dans %s", len(urls), url)
        return urls

    # Vidéo simple
    video_id = info.get("id")
    if video_id:
        return [f"https://www.youtube.com/watch?v={video_id}"]

    raise ValueError(f"Type d'URL non reconnu : {info.get('_type')} pour {url}")


def download_video(
    video_id: str,
    url: str,
    downloads_dir: Path,
    db: Database,
    video_format: str,
) -> dict:
    """
    Télécharge la vidéo source et extrait l'audio en WAV.
    Retourne le dict vidéo mis à jour.
    Idempotent : vérifie l'existence des fichiers avant de relancer.
    """
    video = db.get_video(video_id)
    if video is None:
        raise ValueError(f"Vidéo {video_id} inconnue en base")

    downloads_dir.mkdir(parents=True, exist_ok=True)
    video_path = downloads_dir / f"{video_id}.mp4"
    audio_path = downloads_dir / f"{video_id}.wav"

    # --- Vérification idempotence vidéo ---
    if video_path.exists() and video.get("file_path"):
        logger.info("Vidéo %s déjà téléchargée, étape ignorée", video_id)
    else:
        logger.info("Téléchargement de la vidéo %s…", video_id)
        db.update_video(video_id, status="downloading")

        ydl_opts = {
            "format": video_format,
            "outtmpl": str(downloads_dir / f"{video_id}.%(ext)s"),
            "quiet": False,
            "no_warnings": False,
            "merge_output_format": "mp4",
            "postprocessors": [],
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        # yt-dlp peut choisir une autre extension — on renomme
        actual_path = downloads_dir / f"{video_id}.mp4"
        if not actual_path.exists():
            # chercher le fichier téléchargé avec une autre extension
            candidates = list(downloads_dir.glob(f"{video_id}.*"))
            candidates = [c for c in candidates if c.suffix != ".wav"]
            if candidates:
                candidates[0].rename(actual_path)

        db.update_video(
            video_id,
            file_path=str(actual_path),
            title=info.get("title"),
            channel=info.get("uploader") or info.get("channel"),
            duration=info.get("duration"),
            metadata_json=json.dumps({
                "title": info.get("title"),
                "description": info.get("description", "")[:2000],
                "uploader": info.get("uploader"),
                "channel": info.get("channel"),
                "channel_id": info.get("channel_id"),
                "upload_date": info.get("upload_date"),
                "duration": info.get("duration"),
                "view_count": info.get("view_count"),
                "tags": info.get("tags", []),
                "categories": info.get("categories", []),
                "webpage_url": info.get("webpage_url"),
            }),
            status="downloaded",
        )
        logger.info("Vidéo %s téléchargée : %s", video_id, actual_path)

    # --- Extraction audio ---
    if audio_path.exists() and video.get("audio_path"):
        logger.info("Audio %s déjà extrait, étape ignorée", video_id)
    else:
        logger.info("Extraction audio %s…", video_id)
        _extract_audio(video_path, audio_path)
        db.update_video(video_id, audio_path=str(audio_path))
        logger.info("Audio extrait : %s", audio_path)

    return db.get_video(video_id)


def _extract_audio(video_path: Path, audio_path: Path) -> None:
    """Extrait l'audio d'une vidéo en WAV mono 16 kHz via ffmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vn",                   # pas de vidéo
        "-acodec", "pcm_s16le",  # WAV 16 bits
        "-ar", "16000",          # 16 kHz — suffisant pour la transcription
        "-ac", "1",              # mono
        str(audio_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg a échoué lors de l'extraction audio :\n{result.stderr}"
        )


def fetch_metadata_only(url: str) -> dict:
    """Récupère les métadonnées d'une vidéo sans la télécharger."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return info or {}
