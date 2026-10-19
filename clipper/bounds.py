"""Recalage des bornes de clips sur les frontières de phrases.

Utilise les timestamps mot-à-mot de Deepgram pour aligner début et fin
sur la ponctuation la plus proche. Applique une marge de sécurité configurable.
"""

import logging
import re
from pathlib import Path

from .transcribe import get_word_timestamps, load_transcript

logger = logging.getLogger(__name__)

# Ponctuation qui marque une fin de phrase
SENTENCE_END = re.compile(r"[.!?…]+$")
# Ponctuation qui marque une fin de segment (virgule, point-virgule, etc.)
SEGMENT_END = re.compile(r"[,;:]+$")


def recalibrate_bounds(
    start: float,
    end: float,
    words: list[dict],
    margin: float = 0.3,
    min_duration: float = 120.0,
    max_duration: float = 300.0,
) -> tuple[float, float] | None:
    """
    Recale les bornes start/end sur les frontières de phrases les plus proches.
    Retourne (new_start, new_end) ou None si le clip devient invalide après recalage.

    - start est reculé jusqu'au début du mot qui suit la ponctuation précédente
    - end est avancé jusqu'à la fin du mot de ponctuation le plus proche
    - La marge est soustraite/ajoutée pour un léger fondu

    Args:
        start: timestamp de début proposé (secondes)
        end: timestamp de fin proposé (secondes)
        words: liste de dicts {word, start, end, speaker} issus de Deepgram
        margin: marge en secondes ajoutée/retirée aux bornes
        min_duration: durée minimale acceptable après recalage
        max_duration: durée maximale acceptable après recalage
    """
    if not words:
        # Pas de mots — on applique juste la marge
        return (max(0.0, start - margin), end + margin)

    new_start = _find_sentence_start(start, words, margin)
    new_end = _find_sentence_end(end, words, margin)

    duration = new_end - new_start
    if duration < min_duration:
        logger.warning(
            "Clip [%.0fs-%.0fs] invalide après recalage : durée %.0fs < %.0fs",
            new_start, new_end, duration, min_duration,
        )
        return None

    if duration > max_duration:
        logger.warning(
            "Clip [%.0fs-%.0fs] invalide après recalage : durée %.0fs > %.0fs",
            new_start, new_end, duration, max_duration,
        )
        return None

    logger.debug(
        "Bornes recalées : [%.2f→%.2f] → [%.2f→%.2f] (Δstart=%.2f, Δend=%.2f)",
        start, end, new_start, new_end,
        new_start - start, new_end - end,
    )
    return (new_start, new_end)


def _find_sentence_start(target: float, words: list[dict], margin: float) -> float:
    """
    Trouve le début du mot qui suit la ponctuation de fin de phrase
    la plus proche AVANT target. Si aucune ponctuation n'est trouvée,
    retourne le mot dont le début est le plus proche de target.
    """
    # Chercher dans une fenêtre de ±30s autour de target
    window_start = max(0.0, target - 30.0)
    window_end = target + 5.0

    window = [w for w in words if window_start <= w["start"] <= window_end]
    if not window:
        return max(0.0, target - margin)

    # Chercher la dernière ponctuation forte avant target
    best_idx = None
    for i, w in enumerate(window):
        if w["start"] > target:
            break
        if SENTENCE_END.search(w["word"]):
            best_idx = i

    if best_idx is not None and best_idx + 1 < len(window):
        # Le clip commence au mot qui suit la ponctuation
        next_word = window[best_idx + 1]
        return max(0.0, next_word["start"] - margin)

    # Pas de ponctuation forte — chercher une ponctuation faible
    best_idx = None
    for i, w in enumerate(window):
        if w["start"] > target:
            break
        if SEGMENT_END.search(w["word"]):
            best_idx = i

    if best_idx is not None and best_idx + 1 < len(window):
        next_word = window[best_idx + 1]
        return max(0.0, next_word["start"] - margin)

    # Fallback : mot le plus proche de target
    closest = min(window, key=lambda w: abs(w["start"] - target))
    return max(0.0, closest["start"] - margin)


def _find_sentence_end(target: float, words: list[dict], margin: float) -> float:
    """
    Trouve la fin du mot de ponctuation de fin de phrase
    le plus proche APRÈS target. Si aucun n'est trouvé,
    retourne la fin du mot le plus proche de target.
    """
    window_start = target - 5.0
    window_end = target + 30.0

    window = [w for w in words if window_start <= w["end"] <= window_end]
    if not window:
        # Aucun mot dans la fenêtre — prendre le dernier mot avant la fin
        before = [w for w in words if w["end"] <= target + 5.0]
        if before:
            return before[-1]["end"] + margin
        return target + margin

    # Chercher la première ponctuation forte après target
    for w in window:
        if w["end"] >= target and SENTENCE_END.search(w["word"]):
            return w["end"] + margin

    # Chercher une ponctuation faible
    for w in window:
        if w["end"] >= target and SEGMENT_END.search(w["word"]):
            return w["end"] + margin

    # Fallback : fin du mot le plus proche de target (après)
    after = [w for w in window if w["end"] >= target]
    if after:
        closest = min(after, key=lambda w: abs(w["end"] - target))
        return closest["end"] + margin

    # Dernier recours
    closest = min(window, key=lambda w: abs(w["end"] - target))
    return closest["end"] + margin


def recalibrate_clip(
    clip: dict,
    transcript_path: Path,
    cfg: dict,
) -> tuple[float, float] | None:
    """
    Recale les bornes d'un clip en chargeant le transcript correspondant.
    Retourne (new_start, new_end) ou None si le clip devient invalide.
    """
    transcript = load_transcript(transcript_path)
    words = get_word_timestamps(transcript)

    cutting_cfg = cfg.get("cutting", {})
    sel_cfg = cfg.get("selection", {})

    return recalibrate_bounds(
        start=clip["start_time"],
        end=clip["end_time"],
        words=words,
        margin=cutting_cfg.get("boundary_margin", 0.3),
        min_duration=sel_cfg.get("min_duration", 120.0),
        max_duration=sel_cfg.get("max_duration", 300.0),
    )
