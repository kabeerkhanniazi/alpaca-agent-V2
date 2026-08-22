"""Tests for journal source resolution.

The deployed dashboard has no local journal — data/ is gitignored and Streamlit
Cloud has an ephemeral filesystem. These pin the fallback chain that keeps a
deployed instance populated.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent import journal_source as js


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


@pytest.fixture
def events():
    now = datetime.now(timezone.utc)
    return [
        {"event_type": "trade_approved", "ticker": "SPY", "timestamp": now.isoformat()},
        {"event_type": "cycle_summary", "mode": "DRY RUN", "timestamp": now.isoformat()},
    ]


# ------------------------------------------------------------- parsing

def test_parses_jsonl(events, tmp_path):
    path = write_jsonl(tmp_path / "j.jsonl", events)
    assert len(js.parse_jsonl(path.read_text())) == 2


def test_skips_a_torn_line(events, tmp_path):
    path = tmp_path / "j.jsonl"
    path.write_text(json.dumps(events[0]) + "\n" + '{"broken": ' + "\n")
    assert len(js.parse_jsonl(path.read_text())) == 1


def test_ignores_non_object_lines():
    assert js.parse_jsonl('[1,2,3]\n"a string"\n{"event_type":"x"}') == [{"event_type": "x"}]


def test_blank_input_yields_nothing():
    assert js.parse_jsonl("") == []


# ------------------------------------------------------- resolution order

def test_local_file_wins(events, tmp_path):
    local = write_jsonl(tmp_path / "local.jsonl", events)
    snap = write_jsonl(tmp_path / "snap.jsonl", events[:1])
    payload = js.load_journal(local_path=local, snapshot_path=snap, allow_remote=False)
    assert payload.source == js.SOURCE_LOCAL
    assert payload.is_live


def test_falls_back_to_snapshot_when_no_local(events, tmp_path):
    snap = write_jsonl(tmp_path / "snap.jsonl", events)
    payload = js.load_journal(
        local_path=tmp_path / "missing.jsonl", snapshot_path=snap, allow_remote=False
    )
    assert payload.source == js.SOURCE_SNAPSHOT
    assert not payload.is_live
    assert len(payload.events) == 2


def test_an_empty_local_file_does_not_shadow_the_snapshot(events, tmp_path):
    """A zero-byte journal is not data — it must not block the fallback."""
    empty = tmp_path / "local.jsonl"
    empty.write_text("")
    snap = write_jsonl(tmp_path / "snap.jsonl", events)
    payload = js.load_journal(local_path=empty, snapshot_path=snap, allow_remote=False)
    assert payload.source == js.SOURCE_SNAPSHOT


def test_everything_missing_yields_an_empty_payload(tmp_path):
    """No journal anywhere is an empty state, never an exception."""
    payload = js.load_journal(
        local_path=tmp_path / "a.jsonl",
        snapshot_path=tmp_path / "b.jsonl",
        allow_remote=False,
    )
    assert payload.source == js.SOURCE_NONE
    assert payload.events == []
    assert payload.latest_event_at is None


def test_an_unreachable_remote_does_not_raise(events, tmp_path):
    """Offline is an expected state for a deployed app, not an error."""
    snap = write_jsonl(tmp_path / "snap.jsonl", events)
    payload = js.load_journal(
        local_path=tmp_path / "missing.jsonl",
        url="http://127.0.0.1:9/never-listening.jsonl",
        snapshot_path=snap,
        allow_remote=True,
    )
    assert payload.source == js.SOURCE_SNAPSHOT


# ------------------------------------------------------------ staleness

def test_age_label_reports_freshness(tmp_path):
    now = datetime.now(timezone.utc)
    path = write_jsonl(tmp_path / "j.jsonl", [
        {"event_type": "x", "timestamp": (now - timedelta(hours=3)).isoformat()},
    ])
    payload = js.load_journal(local_path=path, allow_remote=False)
    assert payload.age_label == "3h ago"


def test_recent_events_read_as_just_now(tmp_path):
    now = datetime.now(timezone.utc)
    path = write_jsonl(tmp_path / "j.jsonl", [{"event_type": "x", "timestamp": now.isoformat()}])
    assert js.load_journal(local_path=path, allow_remote=False).age_label == "just now"


def test_naive_timestamps_are_treated_as_utc(tmp_path):
    """Older journal lines were written without an offset."""
    path = write_jsonl(tmp_path / "j.jsonl", [
        {"event_type": "x", "timestamp": datetime.now(timezone.utc)
         .replace(tzinfo=None).isoformat()},
    ])
    payload = js.load_journal(local_path=path, allow_remote=False)
    assert payload.latest_event_at is not None


def test_unparseable_timestamps_do_not_crash(tmp_path):
    path = write_jsonl(tmp_path / "j.jsonl", [{"event_type": "x", "timestamp": "nonsense"}])
    payload = js.load_journal(local_path=path, allow_remote=False)
    assert payload.age_label == "no events"


# ---------------------------------------------------------------- config

def test_remote_url_is_overridable_by_env(monkeypatch):
    monkeypatch.setenv("JOURNAL_REMOTE_URL", "https://example.test/j.jsonl")
    assert js.remote_url() == "https://example.test/j.jsonl"


def test_remote_url_defaults_to_the_data_branch(monkeypatch):
    monkeypatch.delenv("JOURNAL_REMOTE_URL", raising=False)
    assert js.remote_url().startswith("https://raw.githubusercontent.com/")
