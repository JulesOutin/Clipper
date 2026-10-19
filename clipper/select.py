"""Sélection des clips via l'API Claude.

Envoie la transcription horodatée à Claude et récupère une liste de clips
candidats avec début, fin, sujet, note et raison.

Pour les transcriptions longues, découpe en blocs avec overlap avant d'envoyer,
puis fusionne et dédoublonne les résultats.
"""

import json
import logging
from pathlib import Path

import anthropic

from .config import get_api_key
from .db import Database
from .transcribe import format_transcript_for_claude, get_word_timestamps, load_transcript

logger = logging.getLogger(__name__)

# Tarifs Claude (USD par million de tokens)
MODEL_PRICING = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-opus-4-8": {"input": 15.0, "output": 75.0},
    "claude-haiku-4-5-20251001": {"input": 0.8, "output": 4.0},
}

# Outil Claude pour la sélection structurée
SELECTION_TOOL = {
    "name": "extract_clips",
    "description": "Extrait les meilleurs clips de la transcription",
    "input_schema": {
        "type": "object",
        "properties": {
            "clips": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "number", "description": "Début en secondes"},
                        "end": {"type": "number", "description": "Fin en secondes"},
                        "subject": {"type": "string", "description": "Sujet en une phrase"},
                        "score": {"type": "integer", "minimum": 0, "maximum": 100},
                        "reason": {"type": "string", "description": "Justification du choix"},
                    },
                    "required": ["start", "end", "subject", "score", "reason"],
                },
            }
        },
        "required": ["clips"],
    },
}


def select_clips(
    video_id: str,
    db: Database,
    cfg: dict,
) -> list[dict]:
    """
    Sélectionne les clips pour une vidéo.
    Retourne la liste des clips insérés en base.
    """
    video = db.get_video(video_id)
    if video is None:
        raise ValueError(f"Vidéo {video_id} inconnue en base")

    if not video.get("transcript_path"):
        raise ValueError(f"Vidéo {video_id} non transcrite")

    # Vérification idempotence : si des clips candidate existent déjà, on skip
    existing = db.list_clips(video_id=video_id, status="candidate")
    if existing:
        logger.info(
            "Vidéo %s : %d clips candidats déjà présents, sélection ignorée",
            video_id,
            len(existing),
        )
        return existing

    transcript = load_transcript(Path(video["transcript_path"]))
    transcript_text = format_transcript_for_claude(transcript)

    if not transcript_text.strip():
        raise ValueError(f"Transcription vide pour la vidéo {video_id}")

    sel_cfg = cfg.get("selection", {})
    chunk_threshold = sel_cfg.get("chunk_threshold", 300000)

    logger.info("Transcription : %d caractères", len(transcript_text))
    db.update_video(video_id, status="selecting")

    # Découpage si transcription trop longue
    if len(transcript_text) > chunk_threshold:
        logger.info(
            "Transcription trop longue (%d car.) — découpage en blocs",
            len(transcript_text),
        )
        raw_clips = _select_in_chunks(transcript_text, video, cfg, db, video_id)
    else:
        raw_clips = _call_claude_for_clips(transcript_text, video, cfg, db, video_id)

    # Validation et nettoyage
    sel_cfg = cfg.get("selection", {})
    min_dur = sel_cfg.get("min_duration", 120)
    max_dur = sel_cfg.get("max_duration", 300)
    max_overlap = sel_cfg.get("max_overlap_ratio", 0.20)
    min_clips = sel_cfg.get("min_clips", 5)
    max_clips_count = sel_cfg.get("max_clips", 15)

    validated = validate_clips(raw_clips, min_dur, max_dur, max_overlap, video_id)

    if len(validated) < min_clips:
        logger.warning(
            "Vidéo %s : seulement %d clips valides (minimum : %d)",
            video_id,
            len(validated),
            min_clips,
        )

    # Trier par score et limiter
    validated.sort(key=lambda c: c["score"], reverse=True)
    validated = validated[:max_clips_count]

    # Insertion en base
    inserted = []
    for clip in validated:
        row = db.insert_clip(
            video_id,
            start_time=clip["start"],
            end_time=clip["end"],
            duration=clip["end"] - clip["start"],
            subject=clip["subject"],
            score=clip["score"],
            reason=clip["reason"],
        )
        inserted.append(row)

    db.update_video(video_id, status="selected")
    logger.info("Vidéo %s : %d clips candidats insérés en base", video_id, len(inserted))
    return inserted


def _call_claude_for_clips(
    transcript_text: str,
    video: dict,
    cfg: dict,
    db: Database,
    video_id: str,
) -> list[dict]:
    """Appel unique à Claude pour toute la transcription."""
    sel_cfg = cfg.get("selection", {})
    model = sel_cfg.get("model", "claude-sonnet-4-6")
    prompt = _build_prompt(transcript_text, video, cfg)

    api_key = get_api_key("ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=api_key)

    logger.info("Appel Claude (%s) pour la sélection de clips…", model)
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        tools=[SELECTION_TOOL],
        tool_choice={"type": "tool", "name": "extract_clips"},
        messages=[{"role": "user", "content": prompt}],
    )

    _record_claude_cost(response, model, video_id, db)

    return _parse_tool_response(response)


def _select_in_chunks(
    transcript_text: str,
    video: dict,
    cfg: dict,
    db: Database,
    video_id: str,
) -> list[dict]:
    """Découpe la transcription en blocs temporels avec overlap et fusionne les clips."""
    sel_cfg = cfg.get("selection", {})
    chunk_minutes = sel_cfg.get("chunk_size_minutes", 30)
    overlap_minutes = sel_cfg.get("chunk_overlap_minutes", 5)

    # Découpage des lignes en blocs temporels
    blocks = _split_transcript_by_time(transcript_text, chunk_minutes, overlap_minutes)
    logger.info("Transcription découpée en %d blocs", len(blocks))

    all_clips: list[dict] = []
    for i, block_text in enumerate(blocks):
        logger.info("Traitement du bloc %d/%d…", i + 1, len(blocks))
        clips = _call_claude_for_clips(block_text, video, cfg, db, video_id)
        all_clips.extend(clips)

    # Fusion des clips dupliqués (chevauchement > 50%)
    return _merge_duplicate_clips(all_clips)


def _split_transcript_by_time(
    transcript_text: str,
    chunk_minutes: int,
    overlap_minutes: int,
) -> list[str]:
    """
    Découpe le texte de transcription en blocs de chunk_minutes minutes
    avec overlap_minutes minutes de chevauchement.
    Chaque ligne commence par [HH:MM:SS ...].
    """
    lines = transcript_text.strip().split("\n")
    chunk_secs = chunk_minutes * 60
    overlap_secs = overlap_minutes * 60

    # Calcule le timestamp de chaque ligne
    timed_lines: list[tuple[float, str]] = []
    for line in lines:
        ts = _extract_timestamp_from_line(line)
        timed_lines.append((ts, line))

    if not timed_lines:
        return [transcript_text]

    total_duration = timed_lines[-1][0]
    if total_duration <= chunk_secs:
        return [transcript_text]

    blocks = []
    start = 0.0
    while start < total_duration:
        end = start + chunk_secs
        block_lines = [
            line for ts, line in timed_lines if start <= ts < end + overlap_secs
        ]
        if block_lines:
            blocks.append("\n".join(block_lines))
        if end >= total_duration:
            break
        start = end - overlap_secs

    return blocks


def _extract_timestamp_from_line(line: str) -> float:
    """Extrait le timestamp en secondes depuis une ligne "[HH:MM:SS ...]"."""
    if not line.startswith("["):
        return 0.0
    try:
        end = line.index("]")
        time_part = line[1:end].split()[0]
        parts = time_part.split(":")
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + int(s)
    except (ValueError, IndexError):
        pass
    return 0.0


def _merge_duplicate_clips(clips: list[dict]) -> list[dict]:
    """
    Fusionne les clips dupliqués issus de blocs différents.
    Deux clips sont considérés comme doublons si leur chevauchement > 50% du plus court.
    On garde celui avec le meilleur score.
    """
    if len(clips) <= 1:
        return clips

    kept = []
    clips_sorted = sorted(clips, key=lambda c: c["score"], reverse=True)

    for candidate in clips_sorted:
        is_duplicate = False
        for existing in kept:
            overlap = _compute_overlap_ratio(candidate, existing)
            if overlap > 0.50:
                is_duplicate = True
                break
        if not is_duplicate:
            kept.append(candidate)

    return kept


def _compute_overlap_ratio(a: dict, b: dict) -> float:
    """Ratio de chevauchement entre deux clips (relativement au plus court)."""
    start = max(a["start"], b["start"])
    end = min(a["end"], b["end"])
    if end <= start:
        return 0.0
    overlap = end - start
    min_dur = min(a["end"] - a["start"], b["end"] - b["start"])
    if min_dur <= 0:
        return 0.0
    return overlap / min_dur


def _build_prompt(transcript_text: str, video: dict, cfg: dict) -> str:
    """Construit le prompt de sélection depuis prompts/selection.md."""
    prompt_path = Path(__file__).parent.parent / "prompts" / "selection.md"
    template = prompt_path.read_text(encoding="utf-8")

    sel_cfg = cfg.get("selection", {})
    duration = video.get("duration", 0) or 0

    return template.format(
        duration_minutes=round(duration / 60),
        video_title=video.get("title", "Inconnue"),
        channel=video.get("channel", "Inconnu"),
        transcript_text=transcript_text,
        min_clips=sel_cfg.get("min_clips", 5),
        max_clips=sel_cfg.get("max_clips", 15),
        min_duration_seconds=sel_cfg.get("min_duration", 120),
        max_duration_seconds=sel_cfg.get("max_duration", 300),
        min_duration_minutes=sel_cfg.get("min_duration", 120) // 60,
        max_duration_minutes=sel_cfg.get("max_duration", 300) // 60,
        max_overlap_ratio_pct=int(sel_cfg.get("max_overlap_ratio", 0.20) * 100),
        intro_skip_seconds=120,
    )


def _parse_tool_response(response) -> list[dict]:
    """Extrait la liste de clips depuis la réponse tool_use de Claude."""
    for block in response.content:
        if block.type == "tool_use" and block.name == "extract_clips":
            return block.input.get("clips", [])
    raise ParseError("Claude n'a pas appelé l'outil extract_clips")


def _record_claude_cost(response, model: str, video_id: str, db: Database) -> None:
    """Enregistre le coût de l'appel Claude en base."""
    usage = response.usage
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["claude-sonnet-4-6"])
    cost = (
        usage.input_tokens * pricing["input"] / 1_000_000
        + usage.output_tokens * pricing["output"] / 1_000_000
    )
    db.record_cost(
        video_id,
        service="claude",
        amount_usd=cost,
        details={
            "model": model,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_per_mtok_input": pricing["input"],
            "cost_per_mtok_output": pricing["output"],
        },
    )
    logger.info(
        "Coût Claude : %d tok. entrée + %d tok. sortie = $%.4f",
        usage.input_tokens,
        usage.output_tokens,
        cost,
    )


class ParseError(Exception):
    """Levée quand Claude ne renvoie pas le format attendu."""


def validate_clips(
    clips: list[dict],
    min_duration: float,
    max_duration: float,
    max_overlap_ratio: float,
    video_id: str = "",
) -> list[dict]:
    """
    Valide et filtre une liste de clips bruts.
    Retourne uniquement les clips conformes.
    Logue les raisons de rejet.
    """
    if not clips:
        raise ParseError(f"Aucun clip retourné par Claude pour la vidéo {video_id}")

    valid = []
    for clip in clips:
        start = float(clip.get("start", 0))
        end = float(clip.get("end", 0))
        duration = end - start

        if duration < min_duration:
            logger.warning(
                "Clip [%.0fs-%.0fs] rejeté : durée %.0fs < minimum %.0fs (%s)",
                start, end, duration, min_duration, clip.get("subject", ""),
            )
            continue

        if duration > max_duration:
            logger.warning(
                "Clip [%.0fs-%.0fs] rejeté : durée %.0fs > maximum %.0fs (%s)",
                start, end, duration, max_duration, clip.get("subject", ""),
            )
            continue

        if start < 0 or end <= start:
            logger.warning("Clip [%.0fs-%.0fs] rejeté : bornes invalides", start, end)
            continue

        valid.append({"start": start, "end": end, **{k: v for k, v in clip.items() if k not in ("start", "end")}})

    # Vérification des chevauchements
    final = []
    for candidate in sorted(valid, key=lambda c: c.get("score", 0), reverse=True):
        overlapping = False
        for accepted in final:
            ratio = _compute_overlap_ratio(candidate, accepted)
            if ratio > max_overlap_ratio:
                logger.warning(
                    "Clip [%.0fs-%.0fs] rejeté : chevauchement %.0f%% avec [%.0fs-%.0fs]",
                    candidate["start"], candidate["end"],
                    ratio * 100,
                    accepted["start"], accepted["end"],
                )
                overlapping = True
                break
        if not overlapping:
            final.append(candidate)

    if not final:
        raise ParseError(
            f"Aucun clip valide après filtrage pour la vidéo {video_id} "
            f"(durée {min_duration}–{max_duration}s, overlap max {max_overlap_ratio*100:.0f}%)"
        )

    return final
