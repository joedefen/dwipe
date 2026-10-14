"""Unit tests for dwipe.StructuredLogger (entries, filtering, trimming, window)."""
import json
from datetime import datetime, timedelta

import pytest

from dwipe.StructuredLogger import LogEntry, StructuredLogger


# ---------------------------------------------------------------------------
# LogEntry
# ---------------------------------------------------------------------------
def test_log_entry_to_dict_excludes_raw():
    entry = LogEntry(timestamp="2026-01-01T00:00:00", level="MSG",
                     file="f.py", line=1, function="fn", message="hi")
    d = entry.to_dict()
    assert "_raw" not in d
    assert d["message"] == "hi"
    assert d["level"] == "MSG"


def test_log_entry_from_dict_filters_unknown_fields():
    entry = LogEntry.from_dict({
        "timestamp": "2026-01-01T00:00:00", "level": "ERR", "file": "f.py",
        "line": 2, "function": "fn", "unexpected": "drop me",
    })
    assert entry.level == "ERR"
    assert entry.message == ""


def test_display_summary_extracts_json_filebase():
    entry = LogEntry(timestamp="t", level="OK", file="f.py", line=1,
                     function="fn", message='did stuff {"filebase": "video.mp4"}')
    assert entry.display_summary == "video.mp4"


def test_display_summary_falls_back_to_message():
    entry = LogEntry(timestamp="t", level="OK", file="f.py", line=1,
                     function="fn", message="just a plain message")
    assert entry.display_summary == "just a plain message"


# ---------------------------------------------------------------------------
# format_time_delta
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0s"),
        (30, "30s"),
        (90, "1m30s"),
        (3600, "1h0m"),
        (3661, "1h1m"),
    ],
)
def test_format_time_delta(seconds, expected):
    assert LogEntry.format_time_delta(seconds) == expected


def test_format_time_delta_signed():
    assert LogEntry.format_time_delta(-90, signed=True) == "-1m30s"


# ---------------------------------------------------------------------------
# Writing + window reads
# ---------------------------------------------------------------------------
def make_logger(tmp_path):
    return StructuredLogger(app_name="dwipe", log_dir=tmp_path)


def test_write_and_read_window(tmp_path):
    logger = make_logger(tmp_path)
    logger.info("first")
    logger.error("second", data={"x": 1})

    window, state = logger.get_window_of_entries(window_size=10)
    entries = list(window.values())
    assert len(entries) == 2
    assert entries[0].level == "MSG"
    assert entries[1].level == "ERR"
    assert state["file_size"] > 0


def test_refresh_window_reads_only_new_entries(tmp_path):
    logger = make_logger(tmp_path)
    logger.info("a")
    window, state = logger.get_window_of_entries(window_size=10)
    logger.info("b")
    window, state = logger.refresh_window(window, state, window_size=10)
    assert len(window) == 2


# ---------------------------------------------------------------------------
# filter_entries
# ---------------------------------------------------------------------------
def test_filter_entries_shallow(tmp_path):
    logger = make_logger(tmp_path)
    logger.info("alpha message")
    logger.error("beta message")
    entries = list(logger.get_window_of_entries(window_size=10)[0].values())

    filtered, deep = StructuredLogger.filter_entries(entries, "beta", deep=False)
    assert len(filtered) == 1
    assert deep == set()


def test_filter_entries_deep_matches_json_data(tmp_path):
    logger = make_logger(tmp_path)
    logger.event("encoded", data={"filepath": "/videos/movie.mp4"})
    entries = list(logger.get_window_of_entries(window_size=10)[0].values())

    # Shallow match should fail (the filename lives only in structured data)...
    shallow, _ = StructuredLogger.filter_entries(entries, "movie.mp4", deep=False)
    assert shallow == []

    # ...but the deep search finds it.
    deep, deep_matches = StructuredLogger.filter_entries(entries, "movie.mp4", deep=True)
    assert len(deep) == 1
    assert entries[0].timestamp in deep_matches


# ---------------------------------------------------------------------------
# Trimming
# ---------------------------------------------------------------------------
def test_trim_keeps_newest_entries(tmp_path, monkeypatch):
    logger = make_logger(tmp_path)
    for i in range(6):
        logger.info(f"entry-{i}")

    before_lines = logger.log_file.read_text().splitlines()
    assert len(before_lines) == 6

    size = logger.log_file.stat().st_size
    monkeypatch.setattr(logger, "MAX_LOG_SIZE", int(size * 0.6), raising=False)
    monkeypatch.setattr(logger, "TRIM_TO_RATIO", 1.0, raising=False)
    logger._trim_log_file()

    after_lines = logger.log_file.read_text().splitlines()
    assert 0 < len(after_lines) < len(before_lines)
    # The newest entry must survive
    assert "entry-5" in after_lines[-1]


def test_trim_retains_old_errors_over_newer_messages(tmp_path, monkeypatch):
    """ERR entries age 10x slower, so an older error outlives a newer message."""
    logger = make_logger(tmp_path)
    now = datetime.now()
    old_err_ts = (now - timedelta(seconds=1000)).isoformat()   # effective age 100
    newer_msg_ts = (now - timedelta(seconds=200)).isoformat()  # effective age 200

    def entry(ts, level, msg):
        return {"timestamp": ts, "level": level, "file": "f.py", "line": 1,
                "function": "fn", "module": "", "message": msg, "data": {},
                "session_id": "s"}

    err_line = json.dumps(entry(old_err_ts, "ERR", "ancient error"))
    msg_line = json.dumps(entry(newer_msg_ts, "MSG", "recent message"))
    logger.log_file.write_text(err_line + "\n" + msg_line + "\n")

    # Only enough room for a single entry in the trimmed file
    monkeypatch.setattr(logger, "MAX_LOG_SIZE", len(err_line) + 1, raising=False)
    monkeypatch.setattr(logger, "TRIM_TO_RATIO", 1.0, raising=False)
    logger._trim_log_file()

    text = logger.log_file.read_text()
    assert "ancient error" in text
    assert "recent message" not in text
