"""Génération des miniatures pour les clips.

Extrait N frames du clip, sélectionne la plus nette avec un visage détecté,
ajoute le texte avec Pillow et exporte en JPEG ≤ 2 Mo.
"""

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .db import Database

logger = logging.getLogger(__name__)

# Cascade OpenCV pour la détection de visages
try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False
    logger.warning("OpenCV non disponible — sélection par netteté seule (pas de détection visage)")


def generate_thumbnail(
    clip: dict,
    clip_path: Path,
    thumbnails_dir: Path,
    db: Database,
    cfg: dict,
) -> Path:
    """
    Génère la miniature d'un clip.
    Retourne le chemin de l'image générée.
    Idempotent : retourne le chemin existant si déjà générée.
    """
    clip_id = clip["id"]
    thumb_path = thumbnails_dir / f"{clip_id}.jpg"

    if thumb_path.exists() and clip.get("thumbnail_path"):
        logger.info("Miniature %s déjà générée, ignorée", clip_id[:8])
        return thumb_path

    if not clip_path.exists():
        raise FileNotFoundError(f"Clip introuvable : {clip_path}")

    thumbnails_dir.mkdir(parents=True, exist_ok=True)

    thumb_cfg = cfg.get("thumbnail", {})
    num_frames = thumb_cfg.get("num_frames", 10)
    width = thumb_cfg.get("width", 1280)
    height = thumb_cfg.get("height", 720)
    max_size = thumb_cfg.get("max_size_bytes", 2_097_152)

    logger.info("Génération de la miniature pour le clip %s…", clip_id[:8])

    # Extraire les frames candidates
    frames = _extract_frames(clip_path, num_frames, width, height)
    if not frames:
        raise RuntimeError(f"Impossible d'extraire des frames de {clip_path}")

    # Sélectionner la meilleure frame
    best_frame_path = _select_best_frame(frames)

    # Ajouter le texte
    text = clip.get("thumbnail_text", "")
    _add_text_overlay(best_frame_path, thumb_path, text, thumb_cfg, width, height)

    # Vérification taille et recompression si nécessaire
    _ensure_size(thumb_path, max_size)

    # Nettoyage des frames temporaires
    for f in frames:
        try:
            os.unlink(f)
        except OSError:
            pass

    db.update_clip(clip_id, thumbnail_path=str(thumb_path))
    logger.info("Miniature générée : %s (%.0f Ko)", thumb_path.name, thumb_path.stat().st_size / 1024)
    return thumb_path


def _extract_frames(
    clip_path: Path,
    num_frames: int,
    width: int,
    height: int,
) -> list[str]:
    """Extrait num_frames images réparties uniformément dans le clip."""
    # Durée du clip
    duration = _get_clip_duration(clip_path)
    if duration <= 0:
        return []

    tmpdir = tempfile.mkdtemp()
    frames = []

    for i in range(num_frames):
        # Éviter le début et la fin (fondu)
        t = duration * (i + 1) / (num_frames + 1)
        out_path = os.path.join(tmpdir, f"frame_{i:03d}.jpg")
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(t),
            "-i", str(clip_path),
            "-vframes", "1",
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black",
            "-q:v", "2",
            out_path,
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode == 0 and os.path.exists(out_path):
            frames.append(out_path)

    return frames


def _get_clip_duration(clip_path: Path) -> float:
    """Retourne la durée d'un fichier vidéo en secondes."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(clip_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def _sharpness_score(image_path: str) -> float:
    """Calcule la netteté d'une image via la variance du Laplacien."""
    if not _CV2_AVAILABLE:
        return 0.0
    img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return 0.0
    return float(cv2.Laplacian(img, cv2.CV_64F).var())


def _face_count(image_path: str) -> int:
    """Retourne le nombre de visages détectés dans l'image."""
    if not _CV2_AVAILABLE:
        return 0
    img = cv2.imread(image_path)
    if img is None:
        return 0
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # Cascade Haar fournie avec OpenCV
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    if not os.path.exists(cascade_path):
        return 0
    cascade = cv2.CascadeClassifier(cascade_path)
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(50, 50))
    return len(faces) if hasattr(faces, "__len__") else 0


def _select_best_frame(frames: list[str]) -> str:
    """
    Sélectionne la meilleure frame :
    1. En priorité, la plus nette parmi celles avec au moins un visage
    2. Sinon, la plus nette toutes frames confondues
    """
    scored = []
    for path in frames:
        sharpness = _sharpness_score(path)
        faces = _face_count(path)
        scored.append((path, sharpness, faces))

    # Frames avec visage
    with_faces = [(p, s, f) for p, s, f in scored if f > 0]
    if with_faces:
        best = max(with_faces, key=lambda x: x[1])
        logger.debug("Frame sélectionnée avec visage (netteté=%.0f)", best[1])
        return best[0]

    # Fallback : meilleure netteté
    best = max(scored, key=lambda x: x[1])
    logger.debug("Frame sélectionnée par netteté seule (netteté=%.0f)", best[1])
    return best[0]


def _add_text_overlay(
    src_path: str,
    dst_path: Path,
    text: str,
    cfg: dict,
    width: int,
    height: int,
) -> None:
    """Ouvre la frame, ajoute le texte avec Pillow, sauvegarde en JPEG."""
    img = Image.open(src_path).convert("RGB")
    img = img.resize((width, height), Image.LANCZOS)

    if not text:
        img.save(str(dst_path), "JPEG", quality=90)
        return

    draw = ImageDraw.Draw(img)

    font_path = cfg.get("font_path", "")
    font_size = cfg.get("font_size", 60)
    text_color = tuple(cfg.get("text_color", [255, 255, 255]))
    shadow_color = tuple(cfg.get("shadow_color", [0, 0, 0]))
    shadow_offset = tuple(cfg.get("shadow_offset", [3, 3]))

    # Chargement de la police
    font = _load_font(font_path, font_size)

    # Position : centré en bas (80% de la hauteur)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (width - text_w) // 2
    y = int(height * 0.72)

    # Ombre portée
    draw.text((x + shadow_offset[0], y + shadow_offset[1]), text, font=font, fill=shadow_color)
    # Texte principal
    draw.text((x, y), text, font=font, fill=text_color)

    img.save(str(dst_path), "JPEG", quality=90)


def _load_font(font_path: str, size: int):
    """Charge une police TTF ou retombe sur la police PIL par défaut."""
    if font_path and os.path.exists(font_path):
        try:
            return ImageFont.truetype(font_path, size)
        except (IOError, OSError):
            pass

    # Essayer les polices système courantes
    for candidate in [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]:
        if os.path.exists(candidate):
            try:
                return ImageFont.truetype(candidate, size)
            except (IOError, OSError):
                continue

    # Fallback PIL : police bitmap sans taille réglable
    return ImageFont.load_default()


def _ensure_size(path: Path, max_bytes: int) -> None:
    """Recompresse l'image si elle dépasse max_bytes."""
    if path.stat().st_size <= max_bytes:
        return

    quality = 85
    img = Image.open(str(path))
    while quality >= 60:
        img.save(str(path), "JPEG", quality=quality)
        if path.stat().st_size <= max_bytes:
            return
        quality -= 5

    logger.warning("Miniature %s encore trop grande après compression (%.0f Ko)", path.name, path.stat().st_size / 1024)
