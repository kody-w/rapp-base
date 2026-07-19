#!/usr/bin/env python3
"""Fail-closed write gate and guarded pause/resume operator command."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rapp_base.write_control import main


if __name__ == "__main__":
    raise SystemExit(main())
