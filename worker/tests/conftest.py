"""Shared pytest setup.

Adds two paths so worker-internal and shared imports resolve without
installing the packages:

  - `worker/` so that `from worker.X import ...` finds the orchestration
    modules (reviewer, runner, tasks, settings, main).
  - `shared/` so that `from reva.X import ...` finds the shared library
    (types, errors, clients, formatters, db, ...).

Runtime is the same — both `worker/` and `shared/` are added to PYTHONPATH
(or `shared/` is pip-installed) inside the worker container.
"""

from __future__ import annotations

import sys
from pathlib import Path

_WORKER_ROOT = Path(__file__).resolve().parents[1]
_SHARED_ROOT = _WORKER_ROOT.parent / "shared"

for _p in (_WORKER_ROOT, _SHARED_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
