"""Découpe des clips via ffmpeg.

Découpe précise à la frame, force 1080p 16:9, applique un fondu audio
en entrée et sortie, et normalise le volume à -14 LUFS.
"""

import logging
import subprocess
from pathlib import Path

from .db import Database

logger = logging.getLogger(__name__)


def cut_clip(
    clip: dict,
    video_path: Path,
    clips_dir: Path,
    db: Database,
    cfg: dict,
) -> Path:
    """
    Découpe un clip depuis la vidéo source avec ffmpeg.
    Retourne le chemin du clip découpé.
    Idempotent : retourne le chemin existant si déjà découpé.
    """
    clip_id = clip["id"]
    clip_filename = f"{clip_id}.mp4"
    clip_path = clips_dir / clip_filename

    # Idempotence
    if clip_path.exists() and clip.get("clip_path"):
        logger.info("Clip %s déjà découpé, ignoré", clip_id[:8])
        return clip_path

    if not video_path.exists():
        raise FileNotFoundError(f"Vidéo source introuvable : {video_path}")

    clips_dir.mkdir(parents=True, exist_ok=True)

    cutting_cfg = cfg.get("cutting", {})
    audio_fade = cutting_cfg.get("audio_fade", 0.3)
    target_lufs = cutting_cfg.get("target_lufs", -14)
    resolution = cutting_cfg.get("resolution", "1920:1080")
    crf = cutting_cfg.get("crf", 18)

    start = clip["start_time"]
    end = clip["end_time"]
    duration = end - start

    logger.info(
        "Découpe du clip %s [%.1fs-%.1fs = %.0fs]…",
        clip_id[:8], start, end, duration,
    )

    # Normalisation loudness en deux passes
    # Passe 1 : analyse (loudnorm en mode print)
    measured = _measure_loudness(video_path, start, duration)

    # Passe 2 : découpe + correction + fondu
    _ffmpeg_cut(
        video_path=video_path,
        output_path=clip_path,
        start=start,
        duration=duration,
        audio_fade=audio_fade,
        target_lufs=target_lufs,
        resolution=resolution,
        crf=crf,
        measured=measured,
    )

    db.update_clip(clip_id, clip_path=str(clip_path), status="cut")
    logger.info("Clip découpé : %s (%.1f Mo)", clip_path.name, clip_path.stat().st_size / 1e6)
    return clip_path


def _measure_loudness(video_path: Path, start: float, duration: float) -> dict | None:
    """
    Passe 1 loudnorm : mesure les paramètres de loudness du segment.
    Retourne un dict valide ou None si les valeurs sont inutilisables (inf, -inf).
    """
    import re, json as _json

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-t", str(duration),
        "-i", str(video_path),
        "-af", "loudnorm=I=-14:LRA=11:TP=-1:print_format=json",
        "-vn", "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    match = re.search(r"\{[^{}]+\}", result.stderr, re.DOTALL)
    if not match:
        return None
    try:
        data = _json.loads(match.group())
    except _json.JSONDecodeError:
        return None

    # Rejeter si l'une des valeurs clés est inf/-inf (piste audio silencieuse ou corrompue)
    for key in ("input_i", "input_lra", "input_tp", "input_thresh"):
        val = str(data.get(key, ""))
        if "inf" in val.lower():
            logger.warning("loudnorm passe 1 : valeur invalide %s=%s — fallback mono-passe", key, val)
            return None

    return data


def _ffmpeg_cut(
    video_path: Path,
    output_path: Path,
    start: float,
    duration: float,
    audio_fade: float,
    target_lufs: float,
    resolution: str,
    crf: int,
    measured: dict | None,
) -> None:
    """Passe 2 ffmpeg : découpe, mise à l'échelle, fondu et normalisation."""
    # Filtre audio — deux passes si mesure valide, sinon dynaudnorm plus robuste
    if measured:
        i = measured.get("input_i", "-23")
        lra = measured.get("input_lra", "7")
        tp = measured.get("input_tp", "-2")
        thresh = measured.get("input_thresh", "-33")
        offset = measured.get("target_offset", "0")
        loudnorm = (
            f"loudnorm=I={target_lufs}:LRA=11:TP=-1"
            f":measured_I={i}:measured_LRA={lra}:measured_TP={tp}"
            f":measured_thresh={thresh}:offset={offset}:linear=true:print_format=none"
        )
    else:
        # dynaudnorm : normalisation dynamique robuste, ne tue jamais l'audio
        loudnorm = f"dynaudnorm=p=0.9:m=100:s=5"

    afade = f"afade=t=in:st=0:d={audio_fade},afade=t=out:st={duration - audio_fade}:d={audio_fade}"
    audio_filter = f"{afade},{loudnorm}"

    # Filtre vidéo : mise à l'échelle 1080p avec barres noires si nécessaire (letterbox)
    video_filter = (
        f"scale={resolution}:force_original_aspect_ratio=decrease,"
        f"pad={resolution}:(ow-iw)/2:(oh-ih)/2:black"
    )

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-t", str(duration),
        "-i", str(video_path),
        "-vf", video_filter,
        "-af", audio_filter,
        "-c:v", "libx264",
        "-crf", str(crf),
        "-preset", "fast",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",  # optimisé pour le streaming
        str(output_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg a échoué lors de la découpe :\n{result.stderr[-2000:]}"
        )


def cut_all_clips(
    video_id: str,
    db: Database,
    cfg: dict,
    storage_path: Path,
) -> list[dict]:
    """Découpe tous les clips candidats d'une vidéo. Retourne les clips mis à jour."""
    video = db.get_video(video_id)
    if not video or not video.get("file_path"):
        raise ValueError(f"Vidéo {video_id} sans fichier source")

    video_path = Path(video["file_path"])
    clips_dir = storage_path / "clips"
    clips = db.list_clips(video_id=video_id, status="candidate")

    if not clips:
        # Aussi prendre les clips déjà validés mais pas encore découpés
        all_clips = db.list_clips(video_id=video_id)
        clips = [c for c in all_clips if c["status"] in ("candidate", "validated") and not c.get("clip_path")]

    results = []
    for clip in clips:
        try:
            cut_clip(clip, video_path, clips_dir, db, cfg)
            results.append(db.get_clip(clip["id"]))
        except Exception as exc:
            logger.error("Erreur découpe clip %s : %s", clip["id"][:8], exc)
    return results
