"""Tests du recalage des bornes et de la validation de durée."""

import pytest

from clipper.select import validate_clips, ParseError


def test_duration_exactly_at_boundaries():
    """Les clips exactement aux limites doivent être acceptés."""
    clips = [
        {"start": 0.0, "end": 120.0, "score": 80, "subject": "Exactement 2min", "reason": "OK"},
        {"start": 200.0, "end": 500.0, "score": 80, "subject": "Exactement 5min", "reason": "OK"},
        {"start": 600.0, "end": 800.0, "score": 80, "subject": "Clip 3", "reason": "OK"},
        {"start": 900.0, "end": 1100.0, "score": 80, "subject": "Clip 4", "reason": "OK"},
        {"start": 1200.0, "end": 1400.0, "score": 80, "subject": "Clip 5", "reason": "OK"},
    ]
    result = validate_clips(clips, min_duration=120, max_duration=300, max_overlap_ratio=0.20)
    durations = [c["end"] - c["start"] for c in result]
    assert all(120 <= d <= 300 for d in durations)


def test_duration_one_second_too_short():
    """Un clip d'exactement 119s doit être rejeté."""
    clips = [
        {"start": 0.0, "end": 119.0, "score": 80, "subject": "Trop court", "reason": "—"},
    ] + [
        {"start": i * 400, "end": i * 400 + 180, "score": 80, "subject": f"Clip {i}", "reason": "OK"}
        for i in range(1, 6)
    ]
    result = validate_clips(clips, min_duration=120, max_duration=300, max_overlap_ratio=0.20)
    starts = [c["start"] for c in result]
    assert 0.0 not in starts


def test_duration_one_second_too_long():
    """Un clip d'exactement 301s doit être rejeté."""
    clips = [
        {"start": 0.0, "end": 301.0, "score": 80, "subject": "Trop long", "reason": "—"},
    ] + [
        {"start": i * 400, "end": i * 400 + 180, "score": 80, "subject": f"Clip {i}", "reason": "OK"}
        for i in range(1, 6)
    ]
    result = validate_clips(clips, min_duration=120, max_duration=300, max_overlap_ratio=0.20)
    starts = [c["start"] for c in result]
    assert 0.0 not in starts


def test_overlap_exactly_at_threshold():
    """Un chevauchement exactement au seuil (20%) doit être accepté."""
    # Clip A : 0-200 (200s). Clip B : 160-360 (200s).
    # Chevauchement = 40s. Ratio = 40/200 = 20% → à la limite, doit être accepté.
    clips = [
        {"start": 0.0, "end": 200.0, "score": 90, "subject": "Clip A", "reason": "OK"},
        {"start": 160.0, "end": 360.0, "score": 80, "subject": "Clip B", "reason": "OK"},
        {"start": 500.0, "end": 700.0, "score": 70, "subject": "Clip C", "reason": "OK"},
        {"start": 800.0, "end": 1000.0, "score": 70, "subject": "Clip D", "reason": "OK"},
        {"start": 1200.0, "end": 1400.0, "score": 70, "subject": "Clip E", "reason": "OK"},
    ]
    result = validate_clips(clips, min_duration=120, max_duration=300, max_overlap_ratio=0.20)
    starts = {c["start"] for c in result}
    # Les deux clips doivent coexister (overlap = exactement 20%)
    assert 0.0 in starts
    assert 160.0 in starts


def test_overlap_just_above_threshold_rejected():
    """Un chevauchement de 21% doit provoquer le rejet du clip de moindre score."""
    # Clip A : 0-200. Clip B : 158-358. Chevauchement = 42s. Ratio = 42/200 = 21%.
    clips = [
        {"start": 0.0, "end": 200.0, "score": 90, "subject": "Clip A", "reason": "OK"},
        {"start": 158.0, "end": 358.0, "score": 70, "subject": "Clip B", "reason": "OK"},
        {"start": 500.0, "end": 700.0, "score": 70, "subject": "Clip C", "reason": "OK"},
        {"start": 800.0, "end": 1000.0, "score": 70, "subject": "Clip D", "reason": "OK"},
        {"start": 1200.0, "end": 1400.0, "score": 70, "subject": "Clip E", "reason": "OK"},
    ]
    result = validate_clips(clips, min_duration=120, max_duration=300, max_overlap_ratio=0.20)
    starts = {c["start"] for c in result}
    # Clip A gardé (meilleur score), Clip B rejeté
    assert 0.0 in starts
    assert 158.0 not in starts
