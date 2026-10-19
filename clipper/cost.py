"""Estimation et affichage des coûts du pipeline."""

import logging
from typing import Any

from .db import Database

logger = logging.getLogger(__name__)

# Tarifs de référence (USD)
DEEPGRAM_NOVA2_PER_MINUTE = 0.0059
CLAUDE_PRICING = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-opus-4-8": {"input": 15.0, "output": 75.0},
    "claude-haiku-4-5-20251001": {"input": 0.8, "output": 4.0},
}


def estimate_cost(duration_seconds: float, model: str = "claude-sonnet-4-6") -> dict[str, float]:
    """
    Estime le coût d'une vidéo avant traitement.
    Retourne un dict {service: usd_estimé}.
    """
    duration_minutes = duration_seconds / 60.0

    # Deepgram : tarif par minute
    deepgram_cost = duration_minutes * DEEPGRAM_NOVA2_PER_MINUTE

    # Claude : estimation grossière basée sur ~1000 tokens/minute de transcription
    pricing = CLAUDE_PRICING.get(model, CLAUDE_PRICING["claude-sonnet-4-6"])
    estimated_input_tokens = int(duration_minutes * 1000)  # ~1k tokens/min de transcript
    estimated_output_tokens = 2000  # réponse JSON des clips
    claude_cost = (
        estimated_input_tokens * pricing["input"] / 1_000_000
        + estimated_output_tokens * pricing["output"] / 1_000_000
    )

    return {
        "deepgram": deepgram_cost,
        "claude": claude_cost,
        "total": deepgram_cost + claude_cost,
    }


def get_video_cost_summary(video_id: str, db: Database) -> dict[str, Any]:
    """Retourne les coûts réels enregistrés pour une vidéo."""
    costs = db.get_costs(video_id=video_id)
    by_service: dict[str, float] = {}
    for cost in costs:
        service = cost["service"]
        by_service[service] = by_service.get(service, 0.0) + cost["amount_usd"]
    return {
        "by_service": by_service,
        "total": sum(by_service.values()),
    }


def get_total_cost(db: Database) -> float:
    """Retourne le coût total cumulé de tous les traitements."""
    return db.total_cost()
