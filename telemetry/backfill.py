"""Replay salvaged transcripts into the tables.

The live stream is what the runner sees; the transcript is the complete record `exec`
copies off the VM before reaping it (`runner._salvage_transcript`). Because both carry
the same message shape, the same normalizer reads both — which is what makes this file
about twenty lines of real work rather than a second implementation.

Two jobs:

1. **History.** Every run that finished before this layer existed still has its
   transcript on disk, so the tables can start with real data instead of waiting weeks
   to accumulate any.
2. **Validation.** A normalizer is a set of assumptions about a schema nobody promised
   us. Running it over transcripts of real runs is how those assumptions get checked
   against the territory rather than the map.

Idempotent: a run's rows are cleared before replay, and tool calls are keyed on the
runtime's own id, so replaying over what the live stream already wrote converges.

    .venv/bin/python -m telemetry.backfill            # every transcript on disk
    .venv/bin/python -m telemetry.backfill <run_id>   # just one
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from . import config, store
from .normalize import ClaudeCodeAdapter

SUFFIX = ".transcript.jsonl"


def transcripts() -> list[Path]:
    return sorted(config.log_dir.glob(f"*{SUFFIX}"))


async def replay(path: Path) -> dict:
    """Normalize one transcript into rows. Returns what it found."""
    run_id = path.name[: -len(SUFFIX)]
    adapter = ClaudeCodeAdapter(run_id)
    rows = []
    skipped = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rows.extend(adapter.feed(json.loads(line)))
        except Exception:  # noqa: BLE001 - one bad line must not abandon the transcript
            skipped += 1
    rows.extend(adapter.flush())

    await store.clear_run(run_id)
    await store.write(rows)
    return {"run_id": run_id, "rows": len(rows), "turns": adapter.turn, "skipped": skipped}


async def main(argv: list[str]) -> int:
    await store.init()
    paths = [config.log_dir / f"{argv[0]}{SUFFIX}"] if argv else transcripts()
    if not paths:
        print(f"no transcripts in {config.log_dir}")
        return 1
    for path in paths:
        if not path.exists():
            print(f"missing: {path}")
            continue
        result = await replay(path)
        print(
            f"{result['run_id']}: {result['rows']} rows, {result['turns']} turns"
            + (f", {result['skipped']} unparsed lines" if result["skipped"] else "")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
