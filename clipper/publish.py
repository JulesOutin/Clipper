"""Publication des clips validés sur YouTube via l'API Data v3.

Upload en privé avec date programmée, titre, description, tags, miniature et playlist.
Gestion du quota : 10 000 unités/jour, 1 600 par upload = 6 uploads max/jour.
La file de publication est préservée même si le quota est atteint.
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .db import Database
from .config import get_api_key

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

YOUTUBE_QUOTA_PER_DAY = 10_000
UPLOAD_COST_UNITS = 1_600          # coût d'un upload videos.insert
THUMBNAIL_COST_UNITS = 50          # coût thumbnails.set


def _build_youtube_client(token_path: Path):
    """Construit le client YouTube authentifié via OAuth2.
    Lance le flux OAuth dans le navigateur si aucun token n'existe.
    """
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            client_id = get_api_key("YOUTUBE_CLIENT_ID")
            client_secret = get_api_key("YOUTUBE_CLIENT_SECRET")
            client_config = {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            creds = flow.run_local_server(port=0)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())
        logger.info("Token OAuth sauvegardé dans %s", token_path)

    return build("youtube", "v3", credentials=creds)


def check_quota(db: Database, cfg: dict) -> dict:
    """
    Retourne l'état du quota pour aujourd'hui.
    {units_used, units_remaining, uploads_remaining, can_upload}
    """
    pub_cfg = cfg.get("publish", {})
    quota_per_day = pub_cfg.get("youtube_quota_per_day", YOUTUBE_QUOTA_PER_DAY)
    upload_cost = pub_cfg.get("upload_cost_units", UPLOAD_COST_UNITS)

    uploads_today = db.count_uploads_today()
    units_used = uploads_today * (upload_cost + THUMBNAIL_COST_UNITS)
    units_remaining = max(0, quota_per_day - units_used)
    uploads_remaining = units_remaining // (upload_cost + THUMBNAIL_COST_UNITS)

    return {
        "uploads_today": uploads_today,
        "units_used": units_used,
        "units_remaining": units_remaining,
        "uploads_remaining": uploads_remaining,
        "can_upload": uploads_remaining > 0,
        "quota_per_day": quota_per_day,
    }


def schedule_validated_clips(db: Database, cfg: dict) -> list[dict]:
    """
    Ajoute les clips validés à la file de publication avec des dates programmées.
    Respecte la cadence configurée (daily_cadence clips/jour).
    Retourne les entrées ajoutées à la file.
    """
    pub_cfg = cfg.get("publish", {})
    daily_cadence = pub_cfg.get("daily_cadence", 2)

    validated = db.list_clips(status="validated")
    if not validated:
        logger.info("Aucun clip validé à programmer")
        return []

    # Trouver le dernier créneau programmé pour calculer la suite
    queue = db.list_queue()
    pending_queue = [q for q in queue if q["status"] == "pending"]

    if pending_queue:
        last_scheduled = max(
            datetime.fromisoformat(q["scheduled_at"]) for q in pending_queue
        )
    else:
        # Premier clip : demain à 09h00 UTC
        last_scheduled = datetime.now(timezone.utc).replace(
            hour=8, minute=0, second=0, microsecond=0
        )

    added = []
    slot_count = 0
    current_date = last_scheduled.date()
    clips_on_date = sum(
        1 for q in pending_queue
        if datetime.fromisoformat(q["scheduled_at"]).date() == current_date
    )

    for clip in validated:
        # Vérifier que ce clip n'est pas déjà en file
        existing = [q for q in queue if q["clip_id"] == clip["id"] and q["status"] == "pending"]
        if existing:
            logger.debug("Clip %s déjà en file, ignoré", clip["id"][:8])
            continue

        # Calculer le prochain créneau disponible
        if clips_on_date >= daily_cadence:
            current_date += timedelta(days=1)
            clips_on_date = 0

        # Heure de publication : 09h + slot * (24h/cadence)
        hours_offset = int((clips_on_date * 24) / daily_cadence)
        pub_time = datetime.combine(
            current_date,
            datetime.min.time(),
            tzinfo=timezone.utc,
        ).replace(hour=9) + timedelta(hours=hours_offset)

        db.enqueue_clip(clip["id"], pub_time.isoformat())
        clips_on_date += 1
        added.append({"clip_id": clip["id"], "scheduled_at": pub_time.isoformat()})
        logger.info(
            "Clip %s programmé pour le %s",
            clip["id"][:8],
            pub_time.strftime("%Y-%m-%d %H:%M UTC"),
        )

    return added


def publish_validated_clips(db: Database, cfg: dict) -> None:
    """
    Publie les clips en file dont la date est arrivée, dans la limite du quota.
    N'efface jamais la file — les clips non publiés restent pour la prochaine fois.
    """
    # Programmer les clips validés pas encore en file
    schedule_validated_clips(db, cfg)

    quota = check_quota(db, cfg)
    logger.info(
        "Quota YouTube : %d unités utilisées / %d — %d uploads possibles aujourd'hui",
        quota["units_used"], quota["quota_per_day"], quota["uploads_remaining"],
    )

    if not quota["can_upload"]:
        logger.warning(
            "Quota atteint pour aujourd'hui (%d/%d unités). "
            "Les clips restent en file et seront publiés demain.",
            quota["units_used"], quota["quota_per_day"],
        )
        return

    pub_cfg = cfg.get("publish", {})
    token_path = Path(os.getenv("YOUTUBE_TOKEN_PATH", "data/youtube_token.json"))
    if not token_path.is_absolute():
        token_path = Path(__file__).parent.parent / token_path

    try:
        youtube = _build_youtube_client(token_path)
    except Exception as exc:
        logger.error("Impossible d'authentifier YouTube : %s", exc)
        return

    now = datetime.now(timezone.utc)
    pending = [q for q in db.list_queue(status="pending")
               if datetime.fromisoformat(q["scheduled_at"]) <= now]

    if not pending:
        logger.info("Aucun clip à publier maintenant")
        return

    uploaded = 0
    for entry in pending:
        if uploaded >= quota["uploads_remaining"]:
            logger.warning(
                "Quota atteint (%d uploads). %d clip(s) en attente pour demain.",
                uploaded, len(pending) - uploaded,
            )
            break

        clip = db.get_clip(entry["clip_id"])
        if not clip:
            logger.error("Clip %s introuvable pour la publication", entry["clip_id"][:8])
            db.update_queue_item(entry["id"], status="failed", error_msg="Clip introuvable")
            continue

        if not clip.get("clip_path") or not Path(clip["clip_path"]).exists():
            logger.error("Fichier clip manquant pour %s", entry["clip_id"][:8])
            db.update_queue_item(entry["id"], status="failed", error_msg="Fichier manquant")
            continue

        try:
            yt_id = _upload_clip(youtube, clip, entry["scheduled_at"], pub_cfg)
            db.update_clip(
                clip["id"],
                youtube_video_id=yt_id,
                scheduled_at=entry["scheduled_at"],
                status="published",
                published_at=now.isoformat(),
            )
            db.update_queue_item(entry["id"], status="uploaded")
            uploaded += 1
            logger.info(
                "Clip %s publié sur YouTube (ID: %s, programmé: %s)",
                clip["id"][:8], yt_id, entry["scheduled_at"][:10],
            )
        except Exception as exc:
            logger.error("Erreur upload clip %s : %s", clip["id"][:8], exc)
            db.update_queue_item(entry["id"], status="failed", error_msg=str(exc))

    if uploaded:
        remaining = check_quota(db, cfg)
        logger.info(
            "%d clip(s) publiés. Quota restant : %d unités (%d uploads).",
            uploaded, remaining["units_remaining"], remaining["uploads_remaining"],
        )


def _upload_clip(youtube, clip: dict, scheduled_at: str, pub_cfg: dict) -> str:
    """
    Upload le clip sur YouTube en privé avec date de publication programmée.
    Retourne l'ID YouTube de la vidéo uploadée.
    """
    from googleapiclient.http import MediaFileUpload

    title = clip.get("title_selected") or clip.get("subject") or "Clip"
    description = clip.get("description") or ""
    tags = json.loads(clip.get("tags_json") or "[]")
    playlist_id = pub_cfg.get("default_playlist_id", "")

    # Corps de la requête
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags[:500],
            "categoryId": "22",  # Personnes et blogs
            "defaultLanguage": "fr",
            "defaultAudioLanguage": "fr",
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": scheduled_at,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        clip["clip_path"],
        mimetype="video/mp4",
        resumable=True,
        chunksize=10 * 1024 * 1024,  # 10 Mo par chunk
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    # Upload avec reprise automatique
    response = None
    while response is None:
        _, response = request.next_chunk()

    yt_id = response["id"]

    # Upload de la miniature
    if clip.get("thumbnail_path") and Path(clip["thumbnail_path"]).exists():
        try:
            youtube.thumbnails().set(
                videoId=yt_id,
                media_body=MediaFileUpload(clip["thumbnail_path"], mimetype="image/jpeg"),
            ).execute()
        except Exception as exc:
            logger.warning("Miniature non uploadée pour %s : %s", yt_id, exc)

    # Ajout à la playlist
    if playlist_id:
        try:
            youtube.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": yt_id},
                    }
                },
            ).execute()
        except Exception as exc:
            logger.warning("Ajout playlist échoué pour %s : %s", yt_id, exc)

    return yt_id
