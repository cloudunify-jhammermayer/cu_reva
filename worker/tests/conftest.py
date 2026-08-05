"""Shared pytest setup.

Adds two paths so imports resolve without installing the packages:

  - `worker/` so that `from worker.X import ...` finds the orchestration
    modules (reviewer, runner, tasks, settings, main).
  - project root so that `from reva.X import ...` finds the shared library
    (types, errors, clients, formatters, db, ...).

Runtime is the same — both `worker/` and the project root are on PYTHONPATH
(or `reva` is pip-installed from the root) inside the worker container.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_WORKER_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = _WORKER_ROOT.parent

for _p in (_WORKER_ROOT, _PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# The prompts/ directory that actually ships, for tests that assert against
# real prompt files rather than fixtures.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHIPPED_PROMPTS = os.path.join(_REPO_ROOT, "prompts")
