"""Where this layer's data lives.

Deliberately not imported from `control`. The layers share a database and the `run_id`,
and that is the whole contract (`docs/architecture.md` §3) — so the dependency runs one
way only: `control` is the producer and knows about this sink; this sink knows nothing
about `control`. A few lines of duplicated path resolution is the price of being able to
lift this folder into its own service without unpicking an import graph.

These paths must match the ones in `control/config.py`. They are the shared database and
the transcripts `control` salvages, which is exactly the surface §3 says the two layers
have in common.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

db_path = ROOT / "var" / "factory.db"
log_dir = ROOT / "var" / "logs"
