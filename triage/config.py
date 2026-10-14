"""Central paths and settings. Everything can be overridden with env vars."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TRIAGE_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "plant.db"
RAW_DIR = DATA_DIR / "raw"
MANUAL_DIR = DATA_DIR / "manual"
TRACE_PATH = Path(os.environ.get("TRIAGE_TRACE_PATH", ROOT / "logs" / "traces.jsonl"))

MODEL = os.environ.get("TRIAGE_MODEL", "deepseek-v4-flash")
API_KEY = os.environ.get("TRIAGE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = os.environ.get("TRIAGE_BASE_URL", "https://tokenpapa.ai/v1")
MAX_STEPS = int(os.environ.get("TRIAGE_MAX_STEPS", "10"))
SQL_MAX_ROWS = 200
SQL_TIMEOUT_S = 5.0
TOOL_OUTPUT_MAX_CHARS = 8000
