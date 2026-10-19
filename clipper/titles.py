"""Génération des titres, description, tags et texte de miniature via Claude.

Utilise prompts/titles.md comme template, modifiable sans toucher au code.
"""

import json
import logging
from pathlib import Path

import anthropic

from .config import get_api_key
from .db import Database
from .select import MODEL_PRICING, _record_claude_cost
from .transcribe import load_transcript, format_transcript_for_claude

logger = logging.getLogger(__name__)

# Outil Claude pour les titres structurés
TITLES_TOOL = {
    "name": "generate_titles",
    "description": "Génère les métadonnées YouTube pour un clip",
    "input_schema": {
        "type": "object",
        "properties": {
            "titles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "3 propositions de titres (60 car. max chacun)",
                "minItems": 1,
                "maxItems": 3,
            },
            "description": {
                "type": "string",
                "description": "Description YouTube (500 car. max)",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "10 tags YouTube",
                "minItems": 1,
                "maxItems": 10,
            },
            "thumbnail_text": {
                "type": "string",
                "description": "Texte miniature (3-4 mots max)",
            },
        },
        "required": ["titles", "description", "tags", "thumbnail_text"],
    },
}


def generate_titles(
    clip: dict,
    video: dict,
    db: Database,
    cfg: dict,
) -> dict:
    """
    Génère les métadonnées pour un clip et les enregistre en DB.
    Idempotent : skip si titles_json est déjà renseigné.
    Retourne le clip mis à jour.
    """
    clip_id = clip["id"]
    video_id = clip["video_id"]

    if clip.get("titles_json"):
        logger.debug("Clip %s : titres déjà générés, ignoré", clip_id[:8])
        return clip

    if not video.get("transcript_path"):
        logger.warning("Clip %s : pas de transcript, titres impossibles", clip_id[:8])
        return clip

    # Extrait le texte de la portion du clip dans la transcription
    transcript = load_transcript(Path(video["transcript_path"]))
    full_text = format_transcript_for_claude(transcript)
    clip_text = _extract_clip_text(full_text, clip["start_time"], clip["end_time"])

    prompt = _build_titles_prompt(clip, video, clip_text, cfg)

    titles_cfg = cfg.get("titles", {})
    model = titles_cfg.get("model", "claude-sonnet-4-6")

    api_key = get_api_key("ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=api_key)

    logger.info("Génération des titres pour le clip %s…", clip_id[:8])
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        tools=[TITLES_TOOL],
        tool_choice={"type": "tool", "name": "generate_titles"},
        messages=[{"role": "user", "content": prompt}],
    )

    _record_claude_cost(response, model, video_id, db)

    result = _parse_titles_response(response)
    if result is None:
        logger.error("Claude n'a pas retourné de titres pour le clip %s", clip_id[:8])
        return clip

    # Sélectionner le meilleur titre par défaut (le premier)
    titles = result.get("titles", [])
    title_selected = titles[0] if titles else ""

    db.update_clip(
        clip_id,
        titles_json=json.dumps(titles, ensure_ascii=False),
        title_selected=title_selected,
        description=result.get("description", ""),
        tags_json=json.dumps(result.get("tags", []), ensure_ascii=False),
        thumbnail_text=result.get("thumbnail_text", ""),
    )

    logger.info(
        "Clip %s — titre : %s",
        clip_id[:8],
        title_selected[:50],
    )
    return db.get_clip(clip_id)


def _extract_clip_text(full_text: str, start: float, end: float) -> str:
    """Extrait les lignes du transcript qui tombent dans l'intervalle [start, end]."""
    from .transcribe import _fmt_time
    from .select import _extract_timestamp_from_line

    lines = full_text.strip().split("\n")
    result = []
    for line in lines:
        ts = _extract_timestamp_from_line(line)
        if start <= ts <= end:
            result.append(line)
    return "\n".join(result) if result else full_text[:3000]


def _build_titles_prompt(clip: dict, video: dict, clip_text: str, cfg: dict) -> str:
    """Construit le prompt depuis prompts/titles.md."""
    from .transcribe import _fmt_time

    prompt_path = Path(__file__).parent.parent / "prompts" / "titles.md"
    template = prompt_path.read_text(encoding="utf-8")

    titles_cfg = cfg.get("titles", {})
    start_tc = _fmt_time(clip["start_time"])

    # URL source avec timecode
    video_url = video.get("url", "")
    start_sec = int(clip["start_time"])

    return template.format(
        subject=clip.get("subject", ""),
        score=clip.get("score", 0),
        duration_seconds=int(clip.get("duration", 0)),
        video_title=video.get("title", ""),
        video_url=video_url,
        start_timecode=start_tc,
        clip_transcript=clip_text,
        num_proposals=titles_cfg.get("num_proposals", 3),
        max_title_length=titles_cfg.get("max_title_length", 60),
    )


def _parse_titles_response(response) -> dict | None:
    """Extrait le dict de titres depuis la réponse tool_use de Claude."""
    for block in response.content:
        if block.type == "tool_use" and block.name == "generate_titles":
            return block.input
    return None
