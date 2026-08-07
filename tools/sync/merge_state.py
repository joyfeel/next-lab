"""Fold another copy of the state file into the local one.

A run that takes over from a standby starts on an older checkout, so the remote
can carry delivery records this process has never seen — and a plain rebase of
two edits to the same file conflicts, which strands the state locally and gets
every article in it delivered again by the next run.

Every entry here only ever accumulates, and an article that reached a terminal
status must never regress to unknown, so a union is always the correct
reconciliation and it cannot conflict.
"""

import json
import sys
from pathlib import Path

from .config import STATE_FILE

# Higher wins when both sides know an article. `pending` is the only status
# that can still change, so anything else supersedes it.
_RANK = {"notified": 4, "failed": 3, "skipped": 3, "seeded": 2, "pending": 1}


def _rank(record: dict | None) -> int:
    if not record:
        return 0
    return _RANK.get(record.get("status", ""), 0)


def _merge_record(mine: dict | None, theirs: dict | None) -> dict:
    if _rank(theirs) > _rank(mine):
        winner, loser = theirs, mine
    else:
        winner, loser = mine, theirs
    merged = dict(winner or {})
    # Retry counters must not go backwards, or an article stuck failing keeps
    # getting a fresh budget and is retried forever.
    if merged.get("status") == "pending":
        merged["attempts"] = max(
            (winner or {}).get("attempts", 0), (loser or {}).get("attempts", 0)
        )
    return merged


def _posted_at(article_id: str) -> int:
    try:
        return int(article_id.split(".")[1])
    except (IndexError, ValueError):
        return 0


def merge(mine: dict, theirs: dict) -> dict:
    merged = dict(mine)
    seen_mine, seen_theirs = mine.get("seen", {}), theirs.get("seen", {})
    # Sorted, not set-iteration order: this file is rewritten on every poll and
    # committed when it differs, and Python randomises string hashing per
    # process, so an unordered union reshuffles every key each time and lands a
    # full-file commit every 77 seconds.
    keys = sorted(seen_mine.keys() | seen_theirs.keys(), key=lambda k: (_posted_at(k), k))
    merged["seen"] = {
        key: _merge_record(seen_mine.get(key), seen_theirs.get(key)) for key in keys
    }
    # Heartbeat markers are dates, so the later one is the one that happened;
    # taking the earlier would send a second heartbeat for the same day.
    for key in ("last_heartbeat_local_date", "last_heartbeat_date"):
        candidates = [d for d in (mine.get(key), theirs.get(key)) if d]
        if candidates:
            merged[key] = max(candidates)
    # scan_health describes what this process just observed; the other side's
    # view is older by definition.
    if "scan_health" not in merged and "scan_health" in theirs:
        merged["scan_health"] = theirs["scan_health"]
    return merged


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"seen": {}}
    return data if isinstance(data, dict) else {"seen": {}}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m tools.sync.merge_state <other-state.json>")
        return 2
    mine = _load(STATE_FILE)
    theirs = _load(Path(argv[1]))
    merged = merge(mine, theirs)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
    added = len(merged["seen"]) - len(mine.get("seen", {}))
    if added:
        print(f"  merged {added} state entries from the remote")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
