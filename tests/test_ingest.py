"""The single read of the archive: each snapshot fetched once, only for passes that need the day."""

from __future__ import annotations

from collections import Counter

import pytest

from speedmap import days, ingest
from speedmap.files import write_atomic


class Recorder:
    def __init__(self, feed):
        self.feed = feed
        self.rows = []
        self.stats = Counter()

    def add(self, rows):
        self.rows.append(rows)


@pytest.fixture
def archive(monkeypatch):
    """Three snapshots, one of them unreadable; counts every GET."""
    fetched = Counter()
    bodies = {"k1": b"one", "k2": b"two", "bad": b"x"}

    def get_bytes(client, key):
        fetched[key] += 1
        if key == "bad":
            raise OSError("truncated object")
        return bodies[key]

    monkeypatch.setattr(days, "load_for_date", lambda client, date_str: "FEED")
    monkeypatch.setattr(days.r2, "snapshot_keys", lambda client, date_str: list(bodies))
    monkeypatch.setattr(days.r2, "get_bytes", get_bytes)
    monkeypatch.setattr(days, "parse_feed", lambda body: [body.decode()])
    return fetched


def test_every_pass_sees_every_snapshot_from_one_fetch(archive):
    a, b = days.fold_day(None, "2026-09-01", [Recorder, Recorder], workers=2)

    assert archive == {"k1": 1, "k2": 1, "bad": 1}
    assert a.rows == b.rows == [["one"], ["two"], []]
    assert a.feed == "FEED"
    assert a.stats["snapshots"] == 3
    assert a.stats["snapshot_errors"] == b.stats["snapshot_errors"] == 1


def test_a_day_with_no_snapshots_is_none(monkeypatch):
    monkeypatch.setattr(days, "load_for_date", lambda client, date_str: "FEED")
    monkeypatch.setattr(days.r2, "snapshot_keys", lambda client, date_str: [])
    assert days.fold_day(None, "2026-09-01", [Recorder], workers=1) is None


class FakePass:
    def __init__(self, done):
        self.done = done
        self.saved = []

    def is_done(self, date_str):
        return self.done

    def save(self, date_str, day, started):
        self.saved.append(day)
        return True


@pytest.fixture
def passes(monkeypatch):
    speed, seg = FakePass(done=True), FakePass(done=False)
    monkeypatch.setattr(
        ingest, "PASSES", {"speed": (speed, "SpeedDay"), "segments": (seg, "SegmentDay")}
    )
    folded = []

    def fold_day(client, date_str, makers, workers):
        folded.append(list(makers))
        return [f"filled {m}" for m in makers]

    monkeypatch.setattr(ingest, "fold_day", fold_day)
    return speed, seg, folded


def test_only_the_pass_missing_its_output_is_fed(passes):
    speed, seg, folded = passes
    assert ingest.write_day(None, "2026-09-01")
    assert folded == [["SegmentDay"]]
    assert speed.saved == [] and seg.saved == ["filled SegmentDay"]


def test_a_day_both_passes_hold_is_not_downloaded(passes):
    speed, seg, folded = passes
    seg.done = True
    assert not ingest.write_day(None, "2026-09-01")
    assert folded == []


def test_force_with_only_redoes_just_that_pass(passes):
    speed, seg, folded = passes
    ingest.write_day(None, "2026-09-01", only=["speed"], force=True)
    assert folded == [["SpeedDay"]]


def test_an_interrupted_write_leaves_neither_file_nor_temp(tmp_path):
    target = tmp_path / "2026-09-01.parquet"

    def half_write(path):
        path.write_bytes(b"partial")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        write_atomic(target, half_write)
    assert list(tmp_path.iterdir()) == []

    write_atomic(target, lambda path: path.write_bytes(b"whole"))
    assert target.read_bytes() == b"whole"
    assert list(tmp_path.iterdir()) == [target]
