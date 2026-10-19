"""Tests du parsing et de la validation des clips retournés par Claude."""

import pytest

from clipper.select import validate_clips, ParseError


MIN_DUR = 120.0  # 2 min
MAX_DUR = 300.0  # 5 min
MAX_OVERLAP = 0.20


def make_clip(start, end, score=80, subject="Test", reason="Raison"):
    return {"start": start, "end": end, "score": score, "subject": subject, "reason": reason}


# --- Cas nominaux ---

def test_valid_clips_accepted():
    clips = [
        make_clip(0, 180),
        make_clip(300, 480),
        make_clip(600, 780),
        make_clip(900, 1080),
        make_clip(1200, 1380),
    ]
    result = validate_clips(clips, MIN_DUR, MAX_DUR, MAX_OVERLAP)
    assert len(result) == 5


def test_clips_sorted_by_score():
    clips = [
        make_clip(0, 180, score=60),
        make_clip(300, 480, score=90),
        make_clip(600, 780, score=75),
        make_clip(900, 1080, score=85),
        make_clip(1200, 1380, score=70),
    ]
    result = validate_clips(clips, MIN_DUR, MAX_DUR, MAX_OVERLAP)
    scores = [c["score"] for c in result]
    assert scores == sorted(scores, reverse=True)


# --- Rejet pour durée ---

def test_clip_too_short_rejected():
    clips = [
        make_clip(0, 60),   # 60s < 120s → rejeté
        make_clip(200, 380),
        make_clip(500, 680),
        make_clip(700, 880),
        make_clip(900, 1080),
    ]
    result = validate_clips(clips, MIN_DUR, MAX_DUR, MAX_OVERLAP)
    for c in result:
        assert (c["end"] - c["start"]) >= MIN_DUR


def test_clip_too_long_rejected():
    clips = [
        make_clip(0, 400),  # 400s > 300s → rejeté
        make_clip(500, 680),
        make_clip(700, 880),
        make_clip(900, 1080),
        make_clip(1200, 1380),
    ]
    result = validate_clips(clips, MIN_DUR, MAX_DUR, MAX_OVERLAP)
    for c in result:
        assert (c["end"] - c["start"]) <= MAX_DUR


def test_clip_exact_min_duration_accepted():
    clips = [make_clip(0, 120)] + [make_clip(i * 200, i * 200 + 150) for i in range(1, 5)]
    result = validate_clips(clips, MIN_DUR, MAX_DUR, MAX_OVERLAP)
    assert any(abs(c["end"] - c["start"] - 120) < 0.1 for c in result)


def test_clip_exact_max_duration_accepted():
    clips = [make_clip(0, 300)] + [make_clip(i * 400, i * 400 + 150) for i in range(1, 5)]
    result = validate_clips(clips, MIN_DUR, MAX_DUR, MAX_OVERLAP)
    assert any(abs(c["end"] - c["start"] - 300) < 0.1 for c in result)


# --- Rejet pour bornes invalides ---

def test_invalid_bounds_rejected():
    clips = [
        make_clip(100, 50),  # end < start → rejeté
        make_clip(200, 380),
        make_clip(500, 680),
        make_clip(700, 880),
        make_clip(900, 1080),
    ]
    result = validate_clips(clips, MIN_DUR, MAX_DUR, MAX_OVERLAP)
    for c in result:
        assert c["start"] < c["end"]


def test_negative_start_rejected():
    clips = [
        make_clip(-10, 180),  # start négatif → rejeté
        make_clip(300, 480),
        make_clip(600, 780),
        make_clip(900, 1080),
        make_clip(1200, 1380),
    ]
    result = validate_clips(clips, MIN_DUR, MAX_DUR, MAX_OVERLAP)
    for c in result:
        assert c["start"] >= 0


# --- Rejet pour chevauchement ---

def test_overlapping_clips_deduplicated():
    # Deux clips qui se chevauchent à 80% : seul le meilleur score est gardé
    clips = [
        make_clip(0, 200, score=90),
        make_clip(50, 230, score=70),  # chevauchement 75% → rejeté
        make_clip(500, 700),
        make_clip(800, 1000),
        make_clip(1100, 1300),
    ]
    result = validate_clips(clips, MIN_DUR, MAX_DUR, MAX_OVERLAP)
    starts = [c["start"] for c in result]
    # Les deux clips proches ne doivent pas coexister
    assert not (0 in starts and 50 in starts)


def test_non_overlapping_clips_both_kept():
    clips = [
        make_clip(0, 200, score=90),
        make_clip(250, 450, score=80),  # chevauchement 0% → les deux gardés
        make_clip(600, 800),
        make_clip(900, 1080),
        make_clip(1200, 1380),
    ]
    result = validate_clips(clips, MIN_DUR, MAX_DUR, MAX_OVERLAP)
    starts = {c["start"] for c in result}
    assert 0 in starts and 250 in starts


# --- Cas dégénérés ---

def test_empty_clips_raises():
    with pytest.raises(ParseError):
        validate_clips([], MIN_DUR, MAX_DUR, MAX_OVERLAP, video_id="test")


def test_all_invalid_raises():
    clips = [make_clip(0, 10) for _ in range(3)]  # tous trop courts
    with pytest.raises(ParseError):
        validate_clips(clips, MIN_DUR, MAX_DUR, MAX_OVERLAP, video_id="test")


def test_fewer_than_min_logs_warning_but_returns(caplog):
    # validate_clips ne lève pas si < min_clips, c'est pipeline qui logue
    clips = [make_clip(i * 400, i * 400 + 180) for i in range(3)]
    result = validate_clips(clips, MIN_DUR, MAX_DUR, MAX_OVERLAP)
    assert len(result) == 3
