"""Tests de la gestion du quota YouTube et de la file de publication."""

import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from clipper.db import open_db
from clipper.publish import check_quota, schedule_validated_clips, UPLOAD_COST_UNITS, THUMBNAIL_COST_UNITS

QUOTA_PER_DAY = 10_000
COST_PER_UPLOAD = UPLOAD_COST_UNITS + THUMBNAIL_COST_UNITS  # 1650


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        database = open_db(Path(tmpdir))
        yield database
        database.close()


@pytest.fixture
def cfg():
    return {
        "publish": {
            "youtube_quota_per_day": QUOTA_PER_DAY,
            "upload_cost_units": UPLOAD_COST_UNITS,
            "daily_cadence": 2,
            "default_playlist_id": "",
        }
    }


def _make_published_clip(db, video_id="v1", clip_id=None, title="Test"):
    """Insère une vidéo et un clip validé en base."""
    db.upsert_video(video_id, url=f"https://youtube.com/watch?v={video_id}", title=title)
    clip = db.insert_clip(video_id,
                          start_time=0, end_time=180, duration=180,
                          score=80, subject=title, status="validated",
                          clip_path="/tmp/fake.mp4")
    return clip


# ---- Tests check_quota ----

def test_quota_full_at_start(db, cfg):
    """Sans aucun upload, le quota est entier."""
    q = check_quota(db, cfg)
    assert q["units_used"] == 0
    assert q["units_remaining"] == QUOTA_PER_DAY
    assert q["can_upload"] is True
    assert q["uploads_remaining"] == QUOTA_PER_DAY // COST_PER_UPLOAD


def test_quota_decreases_after_upload(db, cfg):
    """Chaque upload enregistré réduit le quota."""
    clip = _make_published_clip(db)
    db.enqueue_clip(clip["id"], datetime.now(timezone.utc).isoformat())
    db.update_queue_item(1, status="uploaded")

    q = check_quota(db, cfg)
    assert q["units_used"] == COST_PER_UPLOAD
    assert q["units_remaining"] == QUOTA_PER_DAY - COST_PER_UPLOAD


def test_quota_blocks_at_6_uploads(db, cfg):
    """À 6 uploads (9 900 unités), le quota est épuisé."""
    # 6 uploads × 1650 = 9 900 → reste 100, insuffisant pour un 7e upload (1650)
    for i in range(6):
        vid = f"v{i}"
        db.upsert_video(vid, url=f"https://youtube.com/watch?v={vid}")
        clip = db.insert_clip(vid, start_time=0, end_time=180, duration=180,
                               score=80, subject=f"Clip {i}", status="validated",
                               clip_path="/tmp/fake.mp4")
        db.enqueue_clip(clip["id"], datetime.now(timezone.utc).isoformat())
        db.update_queue_item(i + 1, status="uploaded")

    q = check_quota(db, cfg)
    assert q["uploads_today"] == 6
    assert q["can_upload"] is False
    assert q["uploads_remaining"] == 0


def test_quota_resets_next_day(db, cfg):
    """count_uploads_today ne compte que les uploads du jour courant."""
    # Simuler un upload d'hier en manipulant directement la DB
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    clip = _make_published_clip(db)
    db.conn.execute(
        "INSERT INTO publish_queue (clip_id, scheduled_at, status, created_at) VALUES (?,?,?,?)",
        (clip["id"], yesterday, "uploaded", yesterday),
    )
    db.conn.commit()

    q = check_quota(db, cfg)
    # L'upload d'hier ne compte pas aujourd'hui
    assert q["uploads_today"] == 0
    assert q["can_upload"] is True


# ---- Tests schedule_validated_clips ----

def test_schedule_adds_clips_to_queue(db, cfg):
    """Les clips validés sont bien ajoutés à la file."""
    clip1 = _make_published_clip(db, "v1", title="Clip 1")
    clip2 = _make_published_clip(db, "v2", title="Clip 2")

    added = schedule_validated_clips(db, cfg)
    assert len(added) == 2

    queue = db.list_queue()
    assert len(queue) == 2


def test_schedule_respects_daily_cadence(db, cfg):
    """Avec cadence=2, 5 clips sont répartis sur 3 jours."""
    for i in range(5):
        _make_published_clip(db, f"v{i}", title=f"Clip {i}")

    schedule_validated_clips(db, cfg)
    queue = db.list_queue()
    assert len(queue) == 5

    # Compter les clips par jour
    by_day: dict = {}
    for entry in queue:
        day = entry["scheduled_at"][:10]
        by_day[day] = by_day.get(day, 0) + 1

    # Aucun jour ne dépasse la cadence de 2
    assert all(count <= 2 for count in by_day.values())
    # 5 clips sur cadence 2 = 3 jours
    assert len(by_day) == 3


def test_schedule_does_not_duplicate(db, cfg):
    """Un clip déjà en file n'est pas ajouté une seconde fois."""
    clip = _make_published_clip(db, "v1")
    schedule_validated_clips(db, cfg)
    schedule_validated_clips(db, cfg)  # deuxième appel

    queue = db.list_queue()
    assert len(queue) == 1


def test_schedule_empty_when_no_validated(db, cfg):
    """Sans clip validé, la file reste vide."""
    added = schedule_validated_clips(db, cfg)
    assert added == []
    assert db.list_queue() == []


# ---- Tests cohérence file ----

def test_failed_uploads_stay_in_queue(db, cfg):
    """Un upload échoué n'est pas retiré de la file, il passe en 'failed'."""
    clip = _make_published_clip(db)
    db.enqueue_clip(clip["id"], datetime.now(timezone.utc).isoformat())
    db.update_queue_item(1, status="failed", error_msg="Erreur réseau")

    queue = db.list_queue()
    assert len(queue) == 1
    assert queue[0]["status"] == "failed"
    assert queue[0]["error_msg"] == "Erreur réseau"

    # Les failed ne comptent pas dans le quota du jour
    q = check_quota(db, cfg)
    assert q["uploads_today"] == 0
