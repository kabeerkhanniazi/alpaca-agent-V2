"""Where the dashboard gets its journal from.

The agent runs on a local machine; the dashboard may be deployed to Streamlit
Cloud, which has an ephemeral filesystem and no scheduler. So the deployed app
can only ever *view* data produced elsewhere, and needs somewhere to read it
from. Three sources are tried in order, most-live first:

1. **Local file** — ``data/options_trades.jsonl``. What a developer running the
   app on the trading machine sees. Always the freshest.
2. **Remote data branch** — a public raw URL, refreshed by the agent every
   cycle. This is what a deployed instance normally reads. Pushing to a branch
   the deploy does not track means the dashboard updates without triggering a
   rebuild.
3. **Committed snapshot** — ``data/snapshot/journal_snapshot.jsonl``, checked
   into the repo. The floor: if the network is down or the push job has stalled,
   judges still see a populated dashboard rather than an empty one.

Every result reports which source produced it and how old it is, so the UI can
say so plainly instead of presenting stale data as live.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = REPO_ROOT / "data" / "snapshot" / "journal_snapshot.jsonl"

# Public raw URL for the journal on the data branch. Overridable via env or
# Streamlit secrets so a fork does not have to edit code.
DEFAULT_REMOTE_URL = (
    "https://raw.githubusercontent.com/"
    "kabeerkhanniazi/alpaca-agent/data/options_trades.jsonl"
)

SOURCE_LOCAL = "local"
SOURCE_REMOTE = "remote"
SOURCE_SNAPSHOT = "snapshot"
SOURCE_NONE = "none"

REMOTE_TIMEOUT_SECONDS = 6


@dataclass
class JournalPayload:
    """Journal events plus the provenance of where they came from."""

    events: list[dict[str, Any]]
    source: str
    detail: str
    fetched_at: datetime
    latest_event_at: datetime | None = None

    @property
    def is_live(self) -> bool:
        return self.source == SOURCE_LOCAL

    @property
    def age_label(self) -> str:
        """How stale the newest event is, in words."""
        if self.latest_event_at is None:
            return "no events"
        delta = datetime.now(timezone.utc) - self.latest_event_at
        seconds = int(delta.total_seconds())
        if seconds < 90:
            return "just now"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"


def parse_jsonl(text: str) -> list[dict[str, Any]]:
    """Parse JSONL text, skipping any line a killed writer left torn."""
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            events.append(record)
    return events


def _latest_timestamp(events: list[dict[str, Any]]) -> datetime | None:
    stamps = []
    for event in events:
        raw = event.get("timestamp")
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        stamps.append(parsed)
    return max(stamps) if stamps else None


def remote_url() -> str:
    """The configured raw URL for the data branch.

    Checks the environment first, then Streamlit secrets when available, so a
    deployment can repoint it without a code change. Reading secrets is guarded
    because ``st.secrets`` raises when no secrets file exists.
    """
    from_env = os.getenv("JOURNAL_REMOTE_URL", "").strip()
    if from_env:
        return from_env
    try:
        import streamlit as st

        value = st.secrets.get("journal_remote_url", "")
        if value:
            return str(value)
    except Exception:  # noqa: BLE001 — no secrets configured is the normal case
        pass
    return DEFAULT_REMOTE_URL


def _load_local(path: Path) -> JournalPayload | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        events = parse_jsonl(path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.warning("Could not read local journal: %s", exc)
        return None
    if not events:
        return None
    return JournalPayload(
        events=events,
        source=SOURCE_LOCAL,
        detail="Live journal on this machine.",
        fetched_at=datetime.now(timezone.utc),
        latest_event_at=_latest_timestamp(events),
    )


def _load_remote(url: str) -> JournalPayload | None:
    try:
        import requests

        response = requests.get(url, timeout=REMOTE_TIMEOUT_SECONDS)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — offline is an expected state
        logger.info("Remote journal unavailable (%s): %s", url, exc)
        return None

    events = parse_jsonl(response.text)
    if not events:
        return None
    return JournalPayload(
        events=events,
        source=SOURCE_REMOTE,
        detail="Synced from the agent's data branch.",
        fetched_at=datetime.now(timezone.utc),
        latest_event_at=_latest_timestamp(events),
    )


def _load_snapshot(path: Path) -> JournalPayload | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        events = parse_jsonl(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    if not events:
        return None
    return JournalPayload(
        events=events,
        source=SOURCE_SNAPSHOT,
        detail="Committed snapshot — the live feed could not be reached.",
        fetched_at=datetime.now(timezone.utc),
        latest_event_at=_latest_timestamp(events),
    )


def load_journal(
    local_path: Path | None = None,
    url: str | None = None,
    snapshot_path: Path | None = None,
    allow_remote: bool = True,
) -> JournalPayload:
    """Resolve the journal from the best available source.

    Never raises. A dashboard that cannot find a journal shows an empty state;
    it does not show a traceback.
    """
    if local_path is not None:
        payload = _load_local(Path(local_path))
        if payload:
            return payload

    if allow_remote:
        payload = _load_remote(url or remote_url())
        if payload:
            return payload

    payload = _load_snapshot(Path(snapshot_path) if snapshot_path else SNAPSHOT_PATH)
    if payload:
        return payload

    return JournalPayload(
        events=[],
        source=SOURCE_NONE,
        detail="No journal found locally, on the data branch, or in the snapshot.",
        fetched_at=datetime.now(timezone.utc),
        latest_event_at=None,
    )
