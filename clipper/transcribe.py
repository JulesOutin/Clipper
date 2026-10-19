"""Transcription audio via l'API Deepgram.

Produit un JSON horodaté au mot avec identification des locuteurs.
Met en cache le résultat — ne re-transcrit jamais deux fois la même vidéo.
"""

import json
import logging
from pathlib import Path

from deepgram import DeepgramClient, PrerecordedOptions, FileSource

from .config import get_api_key
from .db import Database

logger = logging.getLogger(__name__)

# Tarif Deepgram nova-2 avec diarisation (USD par minute)
DEEPGRAM_COST_PER_MINUTE = 0.0059


def transcribe_video(
    video_id: str,
    audio_path: Path,
    downloads_dir: Path,
    db: Database,
    language: str = "fr",
    model: str = "nova-2",
) -> Path:
    """
    Transcrit l'audio et sauvegarde le JSON Deepgram brut.
    Retourne le chemin du fichier transcript.
    Idempotent : retourne le chemin existant si déjà transcrit.
    """
    video = db.get_video(video_id)
    if video is None:
        raise ValueError(f"Vidéo {video_id} inconnue en base")

    transcript_path = downloads_dir / f"{video_id}.transcript.json"

    # Idempotence
    if transcript_path.exists() and video.get("transcript_path"):
        logger.info("Transcription %s déjà présente, étape ignorée", video_id)
        return transcript_path

    if not audio_path.exists():
        raise FileNotFoundError(f"Fichier audio introuvable : {audio_path}")

    logger.info("Transcription de %s avec Deepgram (%s)…", video_id, model)
    db.update_video(video_id, status="transcribing")

    api_key = get_api_key("DEEPGRAM_API_KEY")
    client = DeepgramClient(api_key)

    options = PrerecordedOptions(
        model=model,
        language=language,
        diarize=True,
        utterances=True,
        smart_format=True,
        punctuate=True,
    )

    with open(audio_path, "rb") as f:
        audio_data = f.read()

    payload: FileSource = {"buffer": audio_data}
    response = client.listen.prerecorded.v("1").transcribe_file(payload, options)

    # Sérialisation du résultat
    result_dict = response.to_dict()
    transcript_path.write_text(json.dumps(result_dict, ensure_ascii=False, indent=2), encoding="utf-8")

    # Calcul du coût
    duration_seconds = _get_duration_from_response(result_dict)
    cost_usd = (duration_seconds / 60.0) * DEEPGRAM_COST_PER_MINUTE
    db.record_cost(
        video_id,
        service="deepgram",
        amount_usd=cost_usd,
        details={
            "model": model,
            "language": language,
            "duration_seconds": duration_seconds,
            "cost_per_minute": DEEPGRAM_COST_PER_MINUTE,
        },
    )

    db.update_video(video_id, transcript_path=str(transcript_path), status="transcribed")
    logger.info(
        "Transcription terminée : %s (%.0fs, coût estimé : $%.4f)",
        transcript_path,
        duration_seconds,
        cost_usd,
    )
    return transcript_path


def _get_duration_from_response(response: dict) -> float:
    """Extrait la durée totale en secondes depuis la réponse Deepgram."""
    try:
        return float(
            response["metadata"]["duration"]
        )
    except (KeyError, TypeError, ValueError):
        # Fallback : dernier mot
        try:
            words = response["results"]["channels"][0]["alternatives"][0]["words"]
            if words:
                return float(words[-1].get("end", 0))
        except (KeyError, IndexError):
            pass
    return 0.0


def load_transcript(transcript_path: Path) -> dict:
    """Charge et retourne le JSON Deepgram depuis le disque."""
    with open(transcript_path, "r", encoding="utf-8") as f:
        return json.load(f)


def format_transcript_for_claude(transcript: dict) -> str:
    """
    Convertit la réponse Deepgram en texte lisible pour Claude.
    Format : [HH:MM:SS LOCUTEUR_N] texte

    Utilise les utterances (phrases complètes avec locuteur) si disponibles,
    sinon retombe sur les mots individuels.
    """
    lines = []

    try:
        utterances = transcript["results"]["utterances"]
        if utterances:
            for utt in utterances:
                start = utt.get("start", 0)
                speaker = utt.get("speaker", 0)
                text = utt.get("transcript", "").strip()
                if text:
                    lines.append(f"[{_fmt_time(start)} LOCUTEUR_{speaker}] {text}")
            return "\n".join(lines)
    except (KeyError, TypeError):
        pass

    # Fallback sur les mots
    try:
        words = transcript["results"]["channels"][0]["alternatives"][0]["words"]
        current_speaker = None
        current_start = 0.0
        current_words = []

        for word in words:
            spk = word.get("speaker", 0)
            if spk != current_speaker:
                if current_words:
                    lines.append(
                        f"[{_fmt_time(current_start)} LOCUTEUR_{current_speaker}] "
                        + " ".join(current_words)
                    )
                current_speaker = spk
                current_start = word.get("start", 0)
                current_words = []
            current_words.append(word.get("punctuated_word") or word.get("word", ""))

        if current_words:
            lines.append(
                f"[{_fmt_time(current_start)} LOCUTEUR_{current_speaker}] "
                + " ".join(current_words)
            )
    except (KeyError, TypeError, IndexError):
        pass

    return "\n".join(lines)


def get_word_timestamps(transcript: dict) -> list[dict]:
    """
    Retourne la liste des mots avec leurs timestamps.
    Chaque élément : {word, start, end, speaker}
    """
    try:
        words = transcript["results"]["channels"][0]["alternatives"][0]["words"]
        return [
            {
                "word": w.get("punctuated_word") or w.get("word", ""),
                "start": float(w.get("start", 0)),
                "end": float(w.get("end", 0)),
                "speaker": w.get("speaker", 0),
            }
            for w in words
        ]
    except (KeyError, IndexError, TypeError):
        return []


def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"
