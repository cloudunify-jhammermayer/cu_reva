"""Every REVA_* env var the code reads must be documented in .env.example."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

_SOURCES = [
    _ROOT / "api" / "app" / "settings.py",
    _ROOT / "worker" / "worker" / "settings.py",
    _ROOT / "scheduler" / "scheduler" / "settings.py",
    _ROOT / "reva" / "config.py",
    _ROOT / "reva" / "logging.py",
]

_ALLOWLIST = {
    "REVA_TEST_POSTGRES_URL",
    "REVA_VERIFY_HIGH_COST",
}


def _vars_read_by_code() -> set[str]:
    pattern = re.compile(r"[\"'](REVA_[A-Z0-9_]+)[\"']")
    found: set[str] = set()
    for src in _SOURCES:
        found.update(pattern.findall(src.read_text()))
    return found - _ALLOWLIST


def test_env_example_documents_every_reva_var():
    example = (_ROOT / ".env.example").read_text()
    missing = sorted(v for v in _vars_read_by_code() if v not in example)
    assert not missing, f".env.example is missing: {missing}"
