"""Add scheduler/ and project root to sys.path so imports resolve without installation."""

from __future__ import annotations

import sys
from pathlib import Path

_SCHEDULER_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = _SCHEDULER_ROOT.parent

for _p in (_SCHEDULER_ROOT, _PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
