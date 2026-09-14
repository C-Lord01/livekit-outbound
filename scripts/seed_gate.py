"""Reset the local policy-gate database and load the synthetic demo dataset.

Usage:
    uv run scripts/seed_gate.py

Targets ./data/gate.db unless GATE_DATABASE_URL is set. Drops and recreates
every table, so never point it at anything but a local dev/test database.
"""

from __future__ import annotations

from dotenv import load_dotenv

from livekit_outbound.gate.seed import main

if __name__ == "__main__":
    load_dotenv()
    main()
