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


class Flaky:
    """write_day stand-in: raises for each listed date, that many times."""

    def __init__(self, fail: dict[str, int]):
        self.fail = dict(fail)
        self.calls = []

    def __call__(self, client, date_str, **kwargs):
        self.calls.append(date_str)
        if self.fail.get(date_str, 0) > 0:
            self.fail[date_str] -= 1
            raise OSError("Could not connect to the endpoint URL")


@pytest.fixture
def no_wait(monkeypatch):
    waits = []
    monkeypatch.setattr(days.time, "sleep", waits.append)
    monkeypatch.setattr(days, "DAY_RETRIES", 3)
    monkeypatch.setattr(days, "RETRY_BASE_S", 30.0)
    monkeypatch.setattr(days, "FAIL_STREAK_MAX", 3)
    return waits


def test_a_network_blip_is_retried_with_backoff(no_wait):
    write_day = Flaky({"d1": 2})
    assert days.run_days(write_day, None, ["d1", "d2"], jobs=1) == 0
    assert write_day.calls == ["d1", "d1", "d1", "d2"]
    assert no_wait == [30.0, 60.0]


def test_one_bad_day_does_not_stop_the_run(no_wait):
    write_day = Flaky({"d2": 99})
    assert days.run_days(write_day, None, ["d1", "d2", "d3"], jobs=1) == 1
    assert write_day.calls[-1] == "d3"


def test_failures_in_a_row_stop_the_run(no_wait):
    dead = {d: 99 for d in ["d1", "d2", "d3", "d4", "d5"]}
    write_day = Flaky(dead)
    assert days.run_days(write_day, None, list(dead), jobs=1) == 3
    assert "d4" not in write_day.calls


def test_a_success_resets_the_streak(no_wait):
    write_day = Flaky({"d1": 99, "d2": 99, "d4": 99, "d5": 99})
    assert days.run_days(write_day, None, ["d1", "d2", "d3", "d4", "d5", "d6"], jobs=1) == 4
    assert write_day.calls[-1] == "d6"
