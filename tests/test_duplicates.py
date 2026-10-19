"""Tests d'idempotence — clipper add ne duplique pas les vidéos."""

import tempfile
from pathlib import Path

import pytest

from clipper.db import open_db


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        database = open_db(Path(tmpdir))
        yield database
        database.close()


def test_add_same_video_twice_does_not_duplicate(db):
    """upsert_video sur un ID déjà présent ne crée pas de doublon."""
    db.upsert_video("abc123", url="https://youtube.com/watch?v=abc123", title="Test")
    db.upsert_video("abc123", url="https://youtube.com/watch?v=abc123", title="Test modifié")

    videos = db.list_videos()
    assert len(videos) == 1
    # Le titre d'origine est conservé (pas d'écrasement)
    assert videos[0]["title"] == "Test"


def test_add_same_video_twice_status_unchanged(db):
    """Le status d'une vidéo déjà présente n'est pas réinitialisé."""
    db.upsert_video("abc123", url="https://youtube.com/watch?v=abc123")
    db.update_video("abc123", status="transcribed")

    db.upsert_video("abc123", url="https://youtube.com/watch?v=abc123")

    video = db.get_video("abc123")
    assert video["status"] == "transcribed"


def test_add_two_different_videos_both_inserted(db):
    """Deux vidéos différentes sont bien toutes les deux insérées."""
    db.upsert_video("aaa111", url="https://youtube.com/watch?v=aaa111", title="Vidéo 1")
    db.upsert_video("bbb222", url="https://youtube.com/watch?v=bbb222", title="Vidéo 2")

    videos = db.list_videos()
    assert len(videos) == 2
    ids = {v["id"] for v in videos}
    assert ids == {"aaa111", "bbb222"}


def test_clips_not_duplicated_on_reselect(db):
    """Si des clips candidats existent déjà, on ne les réinsère pas."""
    db.upsert_video("vid001", url="https://youtube.com/watch?v=vid001")
    db.insert_clip("vid001", start_time=0, end_time=180, duration=180, score=80, subject="Clip 1")
    db.insert_clip("vid001", start_time=300, end_time=480, duration=180, score=75, subject="Clip 2")

    existing = db.list_clips(video_id="vid001", status="candidate")
    assert len(existing) == 2

    # Un deuxième appel à list_clips ne duplique rien
    existing2 = db.list_clips(video_id="vid001", status="candidate")
    assert len(existing2) == 2


def test_cost_records_accumulate(db):
    """Les coûts s'accumulent correctement pour une vidéo."""
    db.upsert_video("vid002", url="https://youtube.com/watch?v=vid002")
    db.record_cost("vid002", service="deepgram", amount_usd=0.15)
    db.record_cost("vid002", service="claude", amount_usd=0.08)

    costs = db.get_costs(video_id="vid002")
    assert len(costs) == 2

    total = db.total_cost()
    assert abs(total - 0.23) < 0.001


def test_get_video_by_id_returns_none_for_unknown(db):
    result = db.get_video("nonexistent")
    assert result is None


def test_list_pending_excludes_done_and_error(db):
    """list_pending_videos exclut les vidéos en status done ou error."""
    db.upsert_video("v1", url="u1")
    db.upsert_video("v2", url="u2")
    db.upsert_video("v3", url="u3")
    db.update_video("v1", status="done")
    db.update_video("v2", status="error")

    pending = db.list_pending_videos()
    ids = {v["id"] for v in pending}
    assert "v1" not in ids
    assert "v2" not in ids
    assert "v3" in ids
