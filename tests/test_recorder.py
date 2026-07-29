"""Raw recorder and HTTP layer tests."""

from __future__ import annotations

import gzip
import json
import time

import pytest

from thepit.core.clock import FixedClock
from thepit.feeds.http import _endpoint, _truncate
from thepit.feeds.recorder import RawRecorder

# 2027-01-01T00:00:00Z falls inside hour 00, which makes the path assertions
# below readable rather than dependent on when the suite runs.
TS = 1_798_761_600_000


def test_record_writes_gzipped_jsonl(tmp_path):
    r = RawRecorder(tmp_path)
    rel = r.record("yahoo", "bars", '{"x":1}', ts_ms=TS, meta={"url": "u"})

    assert rel is not None
    path = tmp_path / rel
    assert path.exists()

    with gzip.open(path, "rt") as fh:
        row = json.loads(fh.readline())
    assert row["body"] == '{"x":1}'
    assert row["ts_ms"] == TS
    assert row["meta"]["url"] == "u"


def test_path_is_partitioned_by_source_kind_and_hour(tmp_path):
    r = RawRecorder(tmp_path)
    rel = r.record("edgar", "news", "x", ts_ms=TS)
    assert rel is not None
    parts = rel.split("/")
    assert parts[0] == "edgar"
    assert parts[1] == "news"
    assert parts[-1].endswith(".jsonl.gz")


def test_appends_stay_readable_as_one_stream(tmp_path):
    """Appending writes a new gzip member. Concatenated members are a valid
    gzip stream, so the hour file must still read back whole."""
    r = RawRecorder(tmp_path)
    for i in range(5):
        r.record("yahoo", "bars", json.dumps({"i": i}), ts_ms=TS + i)

    rel = r.record("yahoo", "bars", '{"i":5}', ts_ms=TS + 5)
    with gzip.open(tmp_path / rel, "rt") as fh:
        rows = [json.loads(line) for line in fh]
    assert [json.loads(x["body"])["i"] for x in rows] == [0, 1, 2, 3, 4, 5]


def test_same_hour_shares_one_file(tmp_path):
    """Millions of small files is the failure mode this avoids."""
    r = RawRecorder(tmp_path)
    a = r.record("yahoo", "bars", "1", ts_ms=TS)
    b = r.record("yahoo", "bars", "2", ts_ms=TS + 60_000)
    assert a == b
    assert len(list(tmp_path.rglob("*.jsonl.gz"))) == 1


def test_different_hours_split(tmp_path):
    r = RawRecorder(tmp_path)
    r.record("yahoo", "bars", "1", ts_ms=TS)
    r.record("yahoo", "bars", "2", ts_ms=TS + 3_600_000)
    assert len(list(tmp_path.rglob("*.jsonl.gz"))) == 2


def test_disabled_recorder_writes_nothing(tmp_path):
    r = RawRecorder(tmp_path, enabled=False)
    assert r.record("yahoo", "bars", "x", ts_ms=TS) is None
    assert not list(tmp_path.rglob("*"))


def test_recording_failure_never_breaks_the_feed(tmp_path):
    """The data is nice to have; the feed staying up is the requirement."""
    r = RawRecorder(tmp_path / "file-not-a-dir")
    (tmp_path / "file-not-a-dir").write_text("blocking the path")
    assert r.record("yahoo", "bars", "x", ts_ms=TS) is None  # no exception


def test_bytes_payload_is_decoded(tmp_path):
    r = RawRecorder(tmp_path)
    rel = r.record("yahoo", "bars", b'{"x":1}', ts_ms=TS)
    with gzip.open(tmp_path / rel, "rt") as fh:
        assert json.loads(fh.readline())["body"] == '{"x":1}'


def test_disk_usage_and_pruning(tmp_path):
    r = RawRecorder(tmp_path)
    r.record("yahoo", "bars", "x" * 1000, ts_ms=TS)
    assert r.disk_usage_bytes() > 0

    # Backdate the file well past the retention window.
    for p in tmp_path.rglob("*.jsonl.gz"):
        old = time.time() - 40 * 86_400
        import os

        os.utime(p, (old, old))

    freed = r.prune_older_than(days=30)
    assert freed > 0
    assert r.disk_usage_bytes() == 0


def test_prune_keeps_recent_files(tmp_path):
    r = RawRecorder(tmp_path)
    r.record("yahoo", "bars", "x" * 100, ts_ms=TS)
    before = r.disk_usage_bytes()
    assert r.prune_older_than(days=30) == 0
    assert r.disk_usage_bytes() == before


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://x.com/v8/finance/chart/AAPL?range=1d", "/v8/finance/chart/AAPL"),
        ("https://x.com/cgi-bin/browse-edgar?action=getcurrent", "/cgi-bin/browse-edgar"),
        ("https://x.com", "/"),
    ],
)
def test_endpoint_strips_host_and_query(url, expected):
    """fetch_log groups by endpoint; leaving the query in would make every
    symbol its own endpoint and the grouping useless."""
    assert _endpoint(url) == expected


def test_truncate_collapses_whitespace_and_caps_length():
    assert _truncate("a\n\n  b") == "a b"
    long = _truncate("x" * 1000, limit=50)
    assert len(long) == 50 and long.endswith("…")


def test_fixed_clock_is_controllable():
    c = FixedClock(1000)
    assert c.now_ms() == 1000
    c.advance(500)
    assert c.now_ms() == 1500
