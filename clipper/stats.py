"""Suivi des statistiques YouTube à J+2 et J+7.

Récupère les vues et la durée moyenne de visionnage via l'API YouTube Analytics.
Stocke les résultats en base et permet l'export CSV.
"""

import csv
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .db import Database
from .publish import _build_youtube_client, SCOPES

logger = logging.getLogger(__name__)

ANALYTICS_SCOPES = SCOPES + ["https://www.googleapis.com/auth/yt-analytics.readonly"]


def _build_analytics_client(token_path: Path):
    """Construit le client YouTube Analytics."""
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), ANALYTICS_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            from .config import get_api_key
            client_config = {
                "installed": {
                    "client_id": get_api_key("YOUTUBE_CLIENT_ID"),
                    "client_secret": get_api_key("YOUTUBE_CLIENT_SECRET"),
                    "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, ANALYTICS_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("youtubeAnalytics", "v2", credentials=creds)


def fetch_and_store_stats(db: Database, cfg: dict) -> int:
    """
    Récupère les stats pour tous les clips publiés qui n'ont pas encore
    leurs vues J+2 ou J+7 renseignées.
    Retourne le nombre de clips mis à jour.
    """
    token_path = Path(os.getenv("YOUTUBE_TOKEN_PATH", "data/youtube_token.json"))
    if not token_path.is_absolute():
        token_path = Path(__file__).parent.parent / token_path

    published = [c for c in db.list_clips(status="published") if c.get("youtube_video_id")]
    if not published:
        logger.info("Aucun clip publié avec ID YouTube")
        return 0

    try:
        analytics = _build_analytics_client(token_path)
    except Exception as exc:
        logger.error("Impossible d'authentifier YouTube Analytics : %s", exc)
        return 0

    updated = 0
    now = datetime.now(timezone.utc)

    for clip in published:
        yt_id = clip["youtube_video_id"]
        published_at = clip.get("published_at")
        if not published_at:
            continue

        pub_date = datetime.fromisoformat(published_at)
        days_since = (now - pub_date).days

        needs_2d = clip.get("views_2d") is None and days_since >= 2
        needs_7d = clip.get("views_7d") is None and days_since >= 7

        if not needs_2d and not needs_7d:
            continue

        try:
            if needs_2d:
                stats = _fetch_video_stats(analytics, yt_id, pub_date, pub_date + timedelta(days=2))
                db.update_clip(clip["id"],
                               views_2d=stats["views"],
                               watch_time_2d=stats["watch_time_minutes"])
                logger.info("Clip %s J+2 : %d vues, %.1f min vues moyennes",
                            clip["id"][:8], stats["views"], stats["watch_time_minutes"])

            if needs_7d:
                stats = _fetch_video_stats(analytics, yt_id, pub_date, pub_date + timedelta(days=7))
                db.update_clip(clip["id"],
                               views_7d=stats["views"],
                               watch_time_7d=stats["watch_time_minutes"])
                logger.info("Clip %s J+7 : %d vues, %.1f min vues moyennes",
                            clip["id"][:8], stats["views"], stats["watch_time_minutes"])

            updated += 1
        except Exception as exc:
            logger.error("Erreur stats clip %s (%s) : %s", clip["id"][:8], yt_id, exc)

    return updated


def _fetch_video_stats(analytics, video_id: str, start: datetime, end: datetime) -> dict:
    """Récupère vues et watchTime pour une vidéo sur une période."""
    response = analytics.reports().query(
        ids="channel==MINE",
        startDate=start.strftime("%Y-%m-%d"),
        endDate=end.strftime("%Y-%m-%d"),
        metrics="views,averageViewDuration",
        filters=f"video=={video_id}",
        dimensions="video",
    ).execute()

    rows = response.get("rows", [])
    if rows:
        # colonnes : video, views, averageViewDuration (secondes)
        _, views, avg_duration_sec = rows[0]
        watch_time_minutes = float(avg_duration_sec) / 60.0
        return {"views": int(views), "watch_time_minutes": watch_time_minutes}

    return {"views": 0, "watch_time_minutes": 0.0}


def export_csv(db: Database, output_path: Path) -> None:
    """Exporte les stats de tous les clips publiés en CSV."""
    clips = db.list_clips(status="published")
    if not clips:
        logger.info("Aucun clip publié à exporter")
        return

    fieldnames = [
        "clip_id", "video_id", "title_selected", "subject", "score",
        "duration", "start_time", "end_time",
        "youtube_video_id", "scheduled_at", "published_at",
        "views_2d", "watch_time_2d", "views_7d", "watch_time_7d",
    ]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for clip in clips:
            writer.writerow({k: clip.get(k, "") for k in fieldnames})

    logger.info("Stats exportées : %d clips → %s", len(clips), output_path)
