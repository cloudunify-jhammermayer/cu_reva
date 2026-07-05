# Golden-PR Replay Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **⏸ ON HOLD — do not execute until activated.** Decision (Joseph,
> 2026-07-05): the golden set must come from PRs reviewed by the NEW process.
> Activation checklist: the five pending 2026-07-03…05 plans are landed,
> prompt CHANGELOG is at ≥ the core-knowledge version, and ≥10 new-process PR
> reviews exist across ≥2 repos. **Because this plan is deferred over a moving
> codebase, Task 0 (rebase check) is mandatory and its findings override the
> line numbers/signatures below.**

**Goal:** Replay curated PRs through the pure review pipeline and score precision/recall against version-controlled expectations — measurable prompt/model/engine changes.

**Architecture:** A standalone `eval/` package run inside the worker container: golden YAML entries (repo, PR, pinned SHA, expected/forbidden findings) → `Reviewer.execute()` (no posting) → JSON results → scored markdown report with A/B compare. Bootstrap mines the outcome ledger for candidate expectations.

**Tech Stack:** Python 3.14, PyYAML (already a dep), existing worker context. No new dependencies, no service changes.

**Spec:** `docs/superpowers/specs/2026-07-05-golden-pr-eval-design.md`.

## Global Constraints

- `eval/` lives at the repo root, importable inside the worker container (it is copied by the worker Dockerfile's source COPY — verify in Task 0; if not, add `COPY eval/ ./eval/`).
- Replay must NEVER call `runner.run_review` (posts to GitHub); only `Reviewer.execute(params, ...)`.
- Paid calls: cost estimate + `--yes` gate; spend recorded via `writers.record_claude_spend(db, "eval", cost)`.
- Tests: worker venv (`cd worker && .venv/bin/python -m pytest tests/ -q`), SQLite + fakes; no paid calls in tests. Final gate: worker suite + `ruff check reva worker/worker api/app scheduler/scheduler eval`.
- Golden YAML with `draft: true` is refused by replay.

---

### Task 0: Rebase check (mandatory — the plan is deferred)

**Files:** none modified.

- [ ] **Step 1: Verify the seams this plan builds on**

Run and record the current state of each:

```bash
grep -n "def execute" worker/worker/reviewer.py          # expect: execute(self, params: JobParams, verify_budget_ok: bool = True) -> ReviewResult
grep -n "class JobParams" -A 10 reva/types.py            # expect fields: repository_id, pull_request_id, head_sha, installation_id, review_mode, trigger_event
grep -n "def lookup_pull_request" -A 6 reva/db/writers.py
grep -n "def get_repo_installation_id" reva/github_client.py
grep -n "def build_worker_context" worker/worker/runner.py
grep -n "record_claude_spend" reva/db/writers.py | head -2
grep -n "COPY" worker/Dockerfile                          # is eval/ included in the image?
```

If any signature drifted (likely: `JobParams` grew core-knowledge fields, `execute` gained params), adapt the code in Tasks 2–5 mechanically to the current signatures — the *shape* of the harness (YAML → execute → JSON → score) is the contract, the signatures are not. If `eval/` is not copied into the worker image, add `COPY eval/ ./eval/` next to the existing source COPY lines.

- [ ] **Step 2: Commit nothing; proceed.**

---

### Task 1: Golden-entry schema + loader

**Files:**
- Create: `eval/__init__.py` (empty), `eval/golden/__init__.py`? — no: `eval/golden/` holds YAML only; create `eval/golden_set.py`
- Test: `worker/tests/test_eval_golden_set.py`

**Interfaces:**
- Produces: `eval.golden_set.GoldenEntry` (pydantic: `repo, pr_number, head_sha, review_mode="diff", note="", draft=False, expected: list[Expectation], forbidden: list[Expectation]`), `Expectation` (`file, line, line_tolerance=5, category=None, note=""`), `load_golden_dir(path: Path) -> list[GoldenEntry]` (raises `ValueError` naming the file on any invalid YAML).

- [ ] **Step 1: Write the failing tests**

Create `worker/tests/test_eval_golden_set.py`:

```python
"""Golden-entry YAML schema + loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.golden_set import GoldenEntry, load_golden_dir

_VALID = """\
repo: acme/odoo-modules
pr_number: 214
head_sha: 3f9c2ab7e5d01c9a48b7f1e2d3c4b5a69788e0f1
review_mode: diff
expected:
  - file: custom_addons/x/models/a.py
    line: 87
    category: security
forbidden:
  - file: custom_addons/x/models/a.py
    line: 40
    category: maintainability
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def test_load_valid_entry(tmp_path):
    _write(tmp_path, "acme__odoo-modules__pr214.yml", _VALID)
    entries = load_golden_dir(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e.repo == "acme/odoo-modules" and e.pr_number == 214
    assert e.expected[0].line_tolerance == 5          # default
    assert e.expected[0].category == "security"
    assert e.forbidden[0].line == 40
    assert e.draft is False


def test_draft_flag_roundtrips(tmp_path):
    _write(tmp_path, "d.yml", _VALID + "draft: true\n")
    assert load_golden_dir(tmp_path)[0].draft is True


def test_invalid_yaml_names_the_file(tmp_path):
    _write(tmp_path, "bad.yml", "repo: [unclosed")
    with pytest.raises(ValueError, match="bad.yml"):
        load_golden_dir(tmp_path)


def test_missing_required_field_names_the_file(tmp_path):
    _write(tmp_path, "nofile.yml", "repo: a/b\npr_number: 1\n")
    with pytest.raises(ValueError, match="nofile.yml"):
        load_golden_dir(tmp_path)


def test_empty_dir_is_empty_list(tmp_path):
    assert load_golden_dir(tmp_path) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `cd worker && .venv/bin/python -m pytest tests/test_eval_golden_set.py -q`
Expected: FAIL — `ModuleNotFoundError: eval.golden_set` (ensure repo root is on sys.path via the existing conftest)

- [ ] **Step 3: Implement `eval/golden_set.py`** (and empty `eval/__init__.py`)

```python
"""Golden-entry schema + loader (golden-PR eval spec).

One YAML file per curated PR under eval/golden/. Version-controlled ground
truth: `expected` findings a good review MUST produce, `forbidden` known
false positives it MUST NOT.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Expectation(BaseModel):
    file: str
    line: int
    line_tolerance: int = 5
    category: str | None = None   # None = match any category at the location
    note: str = ""


class GoldenEntry(BaseModel):
    repo: str                     # owner/name
    pr_number: int
    head_sha: str                 # pinned; replay fails the entry if gone
    review_mode: str = "diff"
    note: str = ""
    draft: bool = False           # bootstrap output; replay refuses drafts
    expected: list[Expectation] = Field(default_factory=list)
    forbidden: list[Expectation] = Field(default_factory=list)
    source_file: str = ""         # set by the loader, not the YAML


def load_golden_dir(path: Path) -> list[GoldenEntry]:
    """Load every *.yml under `path`; ValueError names the offending file."""
    entries: list[GoldenEntry] = []
    for f in sorted(path.glob("*.yml")):
        try:
            data = yaml.safe_load(f.read_text()) or {}
            entry = GoldenEntry.model_validate(data)
        except Exception as exc:
            raise ValueError(f"invalid golden entry {f.name}: {exc}") from exc
        entry.source_file = f.name
        entries.append(entry)
    return entries
```

- [ ] **Step 4: Run to verify pass, commit**

Run: `cd worker && .venv/bin/python -m pytest tests/test_eval_golden_set.py -q`

```bash
git add eval/ worker/tests/test_eval_golden_set.py
git commit -m "feat(eval): golden-entry schema + loader"
```

---

### Task 2: Scoring

**Files:**
- Create: `eval/scoring.py`
- Test: `worker/tests/test_eval_scoring.py`

**Interfaces:**
- Consumes: `Expectation` (Task 1); produced findings as plain dicts `{file, line, category, severity, title}`.
- Produces: `score_entry(findings: list[dict], expected: list[Expectation], forbidden: list[Expectation]) -> EntryScore` with `EntryScore = {matched: list[tuple[int, int]], missed_expected: list[int], unmatched_findings: list[int], forbidden_hits: list[tuple[int, int]], precision: float, recall: float}`; `aggregate(scores: dict[str, EntryScore], findings_by_entry) -> dict` (overall + per-category/severity rollups, F1).

- [ ] **Step 1: Write the failing tests**

Create `worker/tests/test_eval_scoring.py`:

```python
"""Pure scoring: matching, tolerance, one-to-one greedy, forbidden hits."""

from __future__ import annotations

from eval.golden_set import Expectation
from eval.scoring import score_entry


def _f(file="a.py", line=10, category="security", severity="major", title="t"):
    return {"file": file, "line": line, "category": category,
            "severity": severity, "title": title}


def _e(file="a.py", line=10, tolerance=5, category="security"):
    return Expectation(file=file, line=line, line_tolerance=tolerance,
                       category=category)


def test_exact_match():
    s = score_entry([_f()], [_e()], [])
    assert s.matched == [(0, 0)]
    assert s.precision == 1.0 and s.recall == 1.0


def test_line_tolerance_boundaries():
    assert score_entry([_f(line=15)], [_e(line=10, tolerance=5)], []).matched
    assert not score_entry([_f(line=16)], [_e(line=10, tolerance=5)], []).matched


def test_category_none_matches_any():
    s = score_entry([_f(category="bug")], [_e(category=None)], [])
    assert s.matched


def test_category_mismatch_no_match():
    s = score_entry([_f(category="bug")], [_e(category="security")], [])
    assert not s.matched and s.missed_expected == [0]


def test_one_to_one_greedy_by_distance():
    findings = [_f(line=12), _f(line=30)]
    expected = [_e(line=10), _e(line=13)]
    s = score_entry(findings, expected, [])
    # finding 0 (line 12) pairs with expectation 1 (line 13, distance 1),
    # expectation 0 (line 10, distance 2) is then unmatched by finding 1 (line 30).
    assert (0, 1) in s.matched
    assert s.missed_expected == [0]
    assert s.unmatched_findings == [1]


def test_forbidden_hit_detected():
    s = score_entry([_f(line=40, category="maintainability")], [],
                    [_e(line=40, category="maintainability")])
    assert s.forbidden_hits == [(0, 0)]


def test_empty_everything():
    s = score_entry([], [], [])
    assert s.precision == 1.0 and s.recall == 1.0  # vacuous truth
```

- [ ] **Step 2: Run to verify failure**

Run: `cd worker && .venv/bin/python -m pytest tests/test_eval_scoring.py -q`
Expected: FAIL — import error

- [ ] **Step 3: Implement `eval/scoring.py`**

```python
"""Pure scoring for golden-PR replays (no I/O, fully unit-tested)."""

from __future__ import annotations

from dataclasses import dataclass, field

from eval.golden_set import Expectation


@dataclass
class EntryScore:
    matched: list[tuple[int, int]] = field(default_factory=list)        # (finding_idx, expected_idx)
    missed_expected: list[int] = field(default_factory=list)
    unmatched_findings: list[int] = field(default_factory=list)
    forbidden_hits: list[tuple[int, int]] = field(default_factory=list)  # (finding_idx, forbidden_idx)
    precision: float = 1.0
    recall: float = 1.0


def _fits(finding: dict, exp: Expectation) -> bool:
    if finding.get("file") != exp.file:
        return False
    line = finding.get("line") or 0
    if abs(line - exp.line) > exp.line_tolerance:
        return False
    return exp.category is None or finding.get("category") == exp.category


def score_entry(
    findings: list[dict],
    expected: list[Expectation],
    forbidden: list[Expectation],
) -> EntryScore:
    """One-to-one greedy matching by line distance."""
    candidates = [
        (abs((f.get("line") or 0) - e.line), fi, ei)
        for fi, f in enumerate(findings)
        for ei, e in enumerate(expected)
        if _fits(f, e)
    ]
    candidates.sort()
    used_f: set[int] = set()
    used_e: set[int] = set()
    matched: list[tuple[int, int]] = []
    for _, fi, ei in candidates:
        if fi in used_f or ei in used_e:
            continue
        matched.append((fi, ei))
        used_f.add(fi)
        used_e.add(ei)

    forbidden_hits = [
        (fi, xi)
        for fi, f in enumerate(findings)
        for xi, x in enumerate(forbidden)
        if _fits(f, x)
    ]
    missed = [ei for ei in range(len(expected)) if ei not in used_e]
    unmatched = [fi for fi in range(len(findings)) if fi not in used_f]
    precision = len(matched) / len(findings) if findings else 1.0
    recall = len(matched) / len(expected) if expected else 1.0
    return EntryScore(matched=matched, missed_expected=missed,
                      unmatched_findings=unmatched, forbidden_hits=forbidden_hits,
                      precision=precision, recall=recall)
```

- [ ] **Step 4: Run to verify pass, commit**

```bash
cd worker && .venv/bin/python -m pytest tests/test_eval_scoring.py -q
git add eval/scoring.py worker/tests/test_eval_scoring.py
git commit -m "feat(eval): one-to-one greedy scoring with forbidden hits"
```

---

### Task 3: Replay runner

**Files:**
- Create: `eval/replay.py`
- Test: `worker/tests/test_eval_replay.py`

**Interfaces:**
- Consumes: `load_golden_dir` (Task 1); `worker.runner.build_worker_context` / `get_context`; `writers.lookup_pull_request` (adapt to its actual signature from Task 0), `ctx.github.get_repo_installation_id`, `Reviewer.execute`, `writers.record_claude_spend`.
- Produces: `replay_entries(ctx, entries, label, results_dir: Path, model: str | None = None) -> dict` writing one JSON per entry (`{status: "ok"|"error"|"skipped_draft", findings: [...], cost_usd, error}`) and returning the summary; CLI `python -m eval.replay --label <l> [--golden-dir eval/golden] [--model m] [--yes]`.

- [ ] **Step 1: Write the failing tests**

Create `worker/tests/test_eval_replay.py`:

```python
"""Replay orchestration with a fake Reviewer — no paid calls, no GitHub posts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.golden_set import GoldenEntry
from eval.replay import replay_entries


def _entry(**over):
    return GoldenEntry(**{
        "repo": "acme/widgets", "pr_number": 7,
        "head_sha": "a" * 40, "source_file": "acme__widgets__pr7.yml",
        **over,
    })


class FakeReviewer:
    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.calls = []

    def execute(self, params, verify_budget_ok=True):
        self.calls.append(params)
        if self._exc:
            raise self._exc
        return self._result


def _ctx(reviewer, monkeypatch, pr_row=(11, 22)):
    """Minimal ctx double + patched lookups (Task 0 pins the real names)."""
    ctx = SimpleNamespace(
        reviewer=reviewer,
        github=SimpleNamespace(get_repo_installation_id=lambda o, r: 555),
        db=object(),
    )
    monkeypatch.setattr("eval.replay._resolve_ids",
                        lambda ctx, repo, pr: pr_row)
    monkeypatch.setattr("eval.replay._record_spend", lambda ctx, cost: None)
    return ctx


def test_happy_path_writes_result(tmp_path, monkeypatch):
    result = SimpleNamespace(
        status="completed", estimated_cost_usd=0.12,
        findings=[SimpleNamespace(file="a.py", line=10, category="bug",
                                  severity="major", title="t",
                                  model_dump=lambda: {"file": "a.py", "line": 10,
                                                      "category": "bug",
                                                      "severity": "major",
                                                      "title": "t"})],
    )
    reviewer = FakeReviewer(result=result)
    ctx = _ctx(reviewer, monkeypatch)

    summary = replay_entries(ctx, [_entry()], "test", tmp_path)

    out = json.loads((tmp_path / "test" / "acme__widgets__pr7.json").read_text())
    assert out["status"] == "ok"
    assert out["findings"][0]["file"] == "a.py"
    assert summary["ok"] == 1
    # JobParams got the pinned SHA + resolved identities.
    p = reviewer.calls[0]
    assert p.head_sha == "a" * 40
    assert p.repository_id == 11 and p.pull_request_id == 22


def test_draft_entries_are_refused(tmp_path, monkeypatch):
    reviewer = FakeReviewer(result=None)
    ctx = _ctx(reviewer, monkeypatch)
    summary = replay_entries(ctx, [_entry(draft=True)], "test", tmp_path)
    assert summary["skipped_draft"] == 1
    assert reviewer.calls == []


def test_execute_error_marks_entry_and_continues(tmp_path, monkeypatch):
    reviewer = FakeReviewer(exc=RuntimeError("SHA gone"))
    ctx = _ctx(reviewer, monkeypatch)
    summary = replay_entries(ctx, [_entry()], "test", tmp_path)
    out = json.loads((tmp_path / "test" / "acme__widgets__pr7.json").read_text())
    assert out["status"] == "error" and "SHA gone" in out["error"]
    assert summary["error"] == 1


def test_missing_pr_row_is_error_not_crash(tmp_path, monkeypatch):
    reviewer = FakeReviewer(result=None)
    ctx = _ctx(reviewer, monkeypatch, pr_row=None)
    summary = replay_entries(ctx, [_entry()], "test", tmp_path)
    assert summary["error"] == 1
    assert reviewer.calls == []
```

- [ ] **Step 2: Run to verify failure**

Run: `cd worker && .venv/bin/python -m pytest tests/test_eval_replay.py -q`
Expected: FAIL — import error

- [ ] **Step 3: Implement `eval/replay.py`**

```python
"""Replay golden PRs through the PURE review pipeline (no posting).

Calls Reviewer.execute directly — never runner.run_review — so nothing is
posted to GitHub, claimed, or notified. Each replay is a PAID CLI call: the
CLI entry prints an estimate and requires --yes.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from eval.golden_set import GoldenEntry, load_golden_dir

logger = structlog.get_logger()

_EST_COST_PER_ENTRY = 0.25  # conservative diff-mode estimate for the --yes prompt


def _resolve_ids(ctx, repo: str, pr_number: int) -> tuple[int, int] | None:
    """(repository_id, pull_request_id) from the deployment DB, or None.

    Task 0 pins the exact lookup: writers.lookup_pull_request (adapt args to
    its real signature; it exists at reva/db/writers.py:717 as of writing).
    """
    from reva.db import writers

    row = writers.lookup_pull_request(ctx.db, repo, pr_number)
    if row is None:
        return None
    return row["repository_id"], row["id"]


def _record_spend(ctx, cost: float | None) -> None:
    from reva.db import writers

    if cost:
        writers.record_claude_spend(ctx.db, "eval", cost)


def replay_entries(ctx, entries: list[GoldenEntry], label: str,
                   results_root: Path, model: str | None = None) -> dict:
    """Run every entry; one JSON per entry; never abort the batch."""
    from reva.types import JobParams

    out_dir = results_root / label
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {"ok": 0, "error": 0, "skipped_draft": 0}

    for entry in entries:
        out_path = out_dir / (Path(entry.source_file).stem + ".json")
        if entry.draft:
            summary["skipped_draft"] += 1
            out_path.write_text(json.dumps(
                {"status": "skipped_draft", "findings": [], "error": None}))
            continue
        try:
            ids = _resolve_ids(ctx, entry.repo, entry.pr_number)
            if ids is None:
                raise RuntimeError(
                    f"{entry.repo}#{entry.pr_number} not in this deployment's DB "
                    f"— replay must run against the deployment that reviewed it"
                )
            owner, name = entry.repo.split("/", 1)
            params = JobParams(
                repository_id=ids[0],
                pull_request_id=ids[1],
                head_sha=entry.head_sha,
                installation_id=ctx.github.get_repo_installation_id(owner, name),
                review_mode=entry.review_mode,
                trigger_event="manual",
            )
            result = ctx.reviewer.execute(params)
            cost = float(getattr(result, "estimated_cost_usd", 0.0) or 0.0)
            _record_spend(ctx, cost)
            findings = [f.model_dump() for f in (result.findings or [])]
            out_path.write_text(json.dumps({
                "status": "ok",
                "review_status": result.status,
                "cost_usd": cost,
                "findings": findings,
                "error": None,
            }, indent=2, default=str))
            summary["ok"] += 1
            logger.info("eval_replay_ok", entry=entry.source_file,
                        findings=len(findings), cost=cost)
        except Exception as exc:
            out_path.write_text(json.dumps(
                {"status": "error", "findings": [], "error": str(exc)}))
            summary["error"] += 1
            logger.warning("eval_replay_error", entry=entry.source_file,
                           error=str(exc))
    return summary


def _main() -> None:
    import argparse

    from worker.runner import build_worker_context, get_context, set_context
    from worker.settings import Settings

    parser = argparse.ArgumentParser(prog="python -m eval.replay")
    parser.add_argument("--label", required=True)
    parser.add_argument("--golden-dir", default="eval/golden", type=Path)
    parser.add_argument("--results-dir", default="eval/results", type=Path)
    parser.add_argument("--model", default=None)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    entries = load_golden_dir(args.golden_dir)
    if args.only:
        entries = [e for e in entries if e.source_file in args.only]
    runnable = [e for e in entries if not e.draft]
    estimate = len(runnable) * _EST_COST_PER_ENTRY
    print(f"{len(runnable)} entries to replay (~${estimate:.2f} estimated). "
          f"{len(entries) - len(runnable)} drafts skipped.")
    if not args.yes:
        print("Re-run with --yes to spend.")
        raise SystemExit(2)

    build_worker_context(Settings.from_env())
    summary = replay_entries(get_context(), entries, args.label,
                             args.results_dir, model=args.model)
    print(summary)


if __name__ == "__main__":
    _main()
```

(Adapter notes from Task 0: `lookup_pull_request`'s real signature/return
keys; whether `Reviewer.execute` accepts a per-call model override — if it
doesn't, `--model` works by exporting `REVA_DEFAULT_MODEL` before
`build_worker_context`, and the CLI should do exactly that:
`os.environ["REVA_DEFAULT_MODEL"] = args.model` when set.)

- [ ] **Step 4: Run to verify pass, commit**

```bash
cd worker && .venv/bin/python -m pytest tests/test_eval_replay.py -q
git add eval/replay.py worker/tests/test_eval_replay.py
git commit -m "feat(eval): replay runner over Reviewer.execute (no posting)"
```

---

### Task 4: Report + compare

**Files:**
- Create: `eval/report.py`
- Test: `worker/tests/test_eval_report.py`

**Interfaces:**
- Consumes: results JSONs (Task 3), golden entries (Task 1), `score_entry` (Task 2).
- Produces: `build_report(golden_dir: Path, results_root: Path, label: str, compare: str | None = None) -> str` (markdown; also written to `results_root/<label>/report.md`); CLI `python -m eval.report --label <l> [--compare <other>]`.

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_eval_report.py`:

```python
"""Report: per-entry scores, rollups, compare deltas."""

from __future__ import annotations

import json
from pathlib import Path

from eval.report import build_report

_GOLDEN = """\
repo: acme/widgets
pr_number: 7
head_sha: {sha}
expected:
  - file: a.py
    line: 10
    category: security
forbidden:
  - file: a.py
    line: 40
    category: maintainability
"""


def _setup(tmp_path: Path, label: str, findings: list[dict]) -> tuple[Path, Path]:
    golden = tmp_path / "golden"
    golden.mkdir(exist_ok=True)
    (golden / "acme__widgets__pr7.yml").write_text(_GOLDEN.format(sha="a" * 40))
    results = tmp_path / "results"
    (results / label).mkdir(parents=True, exist_ok=True)
    (results / label / "acme__widgets__pr7.json").write_text(json.dumps(
        {"status": "ok", "findings": findings, "error": None}))
    return golden, results


def test_report_scores_and_writes_md(tmp_path):
    golden, results = _setup(tmp_path, "base", [
        {"file": "a.py", "line": 12, "category": "security",
         "severity": "major", "title": "t"},
    ])
    md = build_report(golden, results, "base")
    assert "recall" in md.lower()
    assert "1/1" in md or "100" in md          # the expectation was found
    assert (results / "base" / "report.md").exists()


def test_forbidden_hits_are_prominent(tmp_path):
    golden, results = _setup(tmp_path, "base", [
        {"file": "a.py", "line": 40, "category": "maintainability",
         "severity": "minor", "title": "fp"},
    ])
    md = build_report(golden, results, "base")
    assert "forbidden" in md.lower()


def test_compare_shows_both_labels(tmp_path):
    golden, results = _setup(tmp_path, "base", [])
    _setup(tmp_path, "candidate", [
        {"file": "a.py", "line": 10, "category": "security",
         "severity": "major", "title": "t"},
    ])
    md = build_report(golden, results, "candidate", compare="base")
    assert "base" in md and "candidate" in md


def test_error_entries_listed(tmp_path):
    golden, results = _setup(tmp_path, "base", [])
    (results / "base" / "acme__widgets__pr7.json").write_text(json.dumps(
        {"status": "error", "findings": [], "error": "SHA gone"}))
    md = build_report(golden, results, "base")
    assert "SHA gone" in md
```

- [ ] **Step 2: Run to verify failure**

Run: `cd worker && .venv/bin/python -m pytest tests/test_eval_report.py -q`

- [ ] **Step 3: Implement `eval/report.py`**

```python
"""Score results against the golden set and render a markdown report."""

from __future__ import annotations

import json
from pathlib import Path

from eval.golden_set import load_golden_dir
from eval.scoring import score_entry


def _load_results(results_root: Path, label: str) -> dict[str, dict]:
    out = {}
    for f in sorted((results_root / label).glob("*.json")):
        out[f.stem] = json.loads(f.read_text())
    return out


def _score_label(golden_dir: Path, results_root: Path, label: str) -> dict:
    entries = {Path(e.source_file).stem: e for e in load_golden_dir(golden_dir)}
    results = _load_results(results_root, label)
    rows, errors = [], []
    total_matched = total_expected = total_findings = total_forbidden = 0
    for stem, entry in entries.items():
        res = results.get(stem)
        if res is None or res["status"] != "ok":
            errors.append((stem, (res or {}).get("error") or "no result"))
            continue
        # A run that didn't complete (declined/budget/stale) must not score as
        # "missed everything" — list it as unscored instead.
        if res.get("review_status") not in (None, "completed"):
            errors.append((stem, f"review_status={res['review_status']} — not scored"))
            continue
        s = score_entry(res["findings"], entry.expected, entry.forbidden)
        rows.append((stem, s, len(res["findings"])))
        total_matched += len(s.matched)
        total_expected += len(entry.expected)
        total_findings += len(res["findings"])
        total_forbidden += len(s.forbidden_hits)
    recall = total_matched / total_expected if total_expected else 1.0
    precision = total_matched / total_findings if total_findings else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"rows": rows, "errors": errors, "recall": recall,
            "precision": precision, "f1": f1, "forbidden": total_forbidden,
            "matched": total_matched, "expected": total_expected,
            "findings": total_findings}


def _summary_line(label: str, s: dict) -> str:
    return (f"| {label} | {s['matched']}/{s['expected']} "
            f"| {s['recall']:.0%} | {s['precision']:.0%} | {s['f1']:.2f} "
            f"| **{s['forbidden']}** |")


def build_report(golden_dir: Path, results_root: Path, label: str,
                 compare: str | None = None) -> str:
    scored = {label: _score_label(golden_dir, results_root, label)}
    if compare:
        scored[compare] = _score_label(golden_dir, results_root, compare)

    lines = [f"# Golden-PR eval — {label}"
             + (f" vs {compare}" if compare else ""), ""]
    lines += ["| label | matched/expected | recall | precision | F1 | forbidden hits |",
              "|---|---|---|---|---|---|"]
    for lab, s in scored.items():
        lines.append(_summary_line(lab, s))
    lines.append("")

    s = scored[label]
    lines.append("## Per-entry")
    lines += ["| entry | matched | missed | extra findings | forbidden hits |",
              "|---|---|---|---|---|"]
    for stem, es, n_findings in s["rows"]:
        lines.append(f"| {stem} | {len(es.matched)} | {len(es.missed_expected)} "
                     f"| {len(es.unmatched_findings)} | {len(es.forbidden_hits)} |")
    if s["errors"]:
        lines += ["", "## Errors (not scored)"]
        for stem, err in s["errors"]:
            lines.append(f"- `{stem}`: {err}")
    md = "\n".join(lines) + "\n"
    (results_root / label / "report.md").write_text(md)
    return md


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m eval.report")
    parser.add_argument("--label", required=True)
    parser.add_argument("--compare", default=None)
    parser.add_argument("--golden-dir", default="eval/golden", type=Path)
    parser.add_argument("--results-dir", default="eval/results", type=Path)
    args = parser.parse_args()
    print(build_report(args.golden_dir, args.results_dir, args.label,
                       compare=args.compare))


if __name__ == "__main__":
    _main()
```

- [ ] **Step 4: Run to verify pass, commit**

```bash
cd worker && .venv/bin/python -m pytest tests/test_eval_report.py -q
git add eval/report.py worker/tests/test_eval_report.py
git commit -m "feat(eval): scoring report with A/B compare"
```

---

### Task 5: Bootstrap (mine candidates from the outcome ledger)

**Files:**
- Create: `eval/bootstrap.py`
- Test: `worker/tests/test_eval_bootstrap.py`

**Interfaces:**
- Consumes: `review_runs`/`review_findings` ORM models + the outcome ledger fields (Task 0 pins names: `grep -n "outcome" reva/db/models.py` — findings carry an outcome such as `open/resolved_by_fix/dismissed/still_open_at_merge`).
- Produces: `bootstrap_drafts(db, golden_dir: Path, since: datetime, repo: str | None = None, min_findings: int = 1) -> int` (drafts written, `draft: true`, never overwrites non-drafts); CLI `python -m eval.bootstrap --since 2026-07-15 [--repo owner/name]`.

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_eval_bootstrap.py`:

```python
"""Bootstrap mines new-process reviews into draft golden entries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from eval.bootstrap import bootstrap_drafts
from reva.db import Base, Database, create_engine_from_url


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _seed_review(db, *, repo="acme/widgets", pr=7, sha="a" * 40,
                 created=None, outcomes=("resolved_by_fix", "dismissed")):
    """Insert repository + pull_request + completed review_run + findings.

    Task 0 pins the exact model/field names — adapt this helper to them
    (Repository, PullRequest, ReviewRun, ReviewFinding with an outcome
    column). The assertions below are the contract.
    """
    from reva.db.models import PullRequest, Repository, ReviewFinding, ReviewRun

    created = created or datetime.now(timezone.utc)
    with db.session() as s:
        r = Repository(full_name=repo)          # adapt required fields
        s.add(r); s.flush()
        p = PullRequest(repository_id=r.id, pr_number=pr)  # adapt
        s.add(p); s.flush()
        run = ReviewRun(repository_id=r.id, pull_request_id=p.id,
                        head_sha=sha, review_mode="diff",
                        status="completed", created_at=created)  # adapt
        s.add(run); s.flush()
        for i, outcome in enumerate(outcomes):
            s.add(ReviewFinding(review_run_id=run.id, file=f"f{i}.py",
                                line=10 + i, category="bug", severity="major",
                                title=f"finding {i}", outcome=outcome))  # adapt
        return r.id


def test_drafts_written_from_outcomes(db, tmp_path):
    _seed_review(db)
    n = bootstrap_drafts(db, tmp_path, since=datetime.now(timezone.utc) - timedelta(days=1))
    assert n == 1
    data = yaml.safe_load(next(tmp_path.glob("*.yml")).read_text())
    assert data["draft"] is True
    assert data["repo"] == "acme/widgets"
    files_expected = [e["file"] for e in data["expected"]]
    files_forbidden = [e["file"] for e in data["forbidden"]]
    assert "f0.py" in files_expected       # resolved_by_fix
    assert "f1.py" in files_forbidden      # dismissed


def test_old_reviews_excluded(db, tmp_path):
    _seed_review(db, created=datetime.now(timezone.utc) - timedelta(days=90))
    n = bootstrap_drafts(db, tmp_path, since=datetime.now(timezone.utc) - timedelta(days=1))
    assert n == 0


def test_never_overwrites_curated_file(db, tmp_path):
    _seed_review(db)
    curated = tmp_path / "acme__widgets__pr7.yml"
    curated.write_text("repo: acme/widgets\npr_number: 7\nhead_sha: " + "a" * 40 + "\n")
    bootstrap_drafts(db, tmp_path, since=datetime.now(timezone.utc) - timedelta(days=1))
    assert "draft" not in curated.read_text()
```

- [ ] **Step 2: Run to verify failure, then implement `eval/bootstrap.py`**

```python
"""Mine NEW-process reviews into draft golden entries (curate before use)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml
from sqlalchemy import select

# Task 0 pins these names; adapt mechanically if they drifted.
from reva.db.engine import Database
from reva.db.models import PullRequest, Repository, ReviewFinding, ReviewRun

_EXPECTED_OUTCOMES = {"resolved_by_fix"}
_FORBIDDEN_OUTCOMES = {"dismissed"}


def bootstrap_drafts(db: Database, golden_dir: Path, since: datetime,
                     repo: str | None = None, min_findings: int = 1) -> int:
    golden_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    with db.session() as s:
        q = (
            select(ReviewRun, PullRequest, Repository)
            .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
            .join(Repository, ReviewRun.repository_id == Repository.id)
            .where(ReviewRun.status == "completed",
                   ReviewRun.created_at >= since)
        )
        if repo:
            q = q.where(Repository.full_name == repo)
        for run, pr, repo_row in s.execute(q).all():
            findings = s.execute(
                select(ReviewFinding).where(ReviewFinding.review_run_id == run.id)
            ).scalars().all()
            expected = [f for f in findings if f.outcome in _EXPECTED_OUTCOMES]
            forbidden = [f for f in findings if f.outcome in _FORBIDDEN_OUTCOMES]
            if len(expected) + len(forbidden) < min_findings:
                continue
            name = f"{repo_row.full_name.replace('/', '__')}__pr{pr.pr_number}.yml"
            out = golden_dir / name
            if out.exists():
                continue  # never overwrite curated (or prior draft) files
            out.write_text(yaml.safe_dump({
                "repo": repo_row.full_name,
                "pr_number": pr.pr_number,
                "head_sha": run.head_sha,
                "review_mode": run.review_mode,
                "draft": True,
                "note": f"bootstrap from run {run.id} — CURATE THEN REMOVE draft",
                "expected": [
                    {"file": f.file, "line": f.line, "category": f.category,
                     "note": f.title} for f in expected
                ],
                "forbidden": [
                    {"file": f.file, "line": f.line, "category": f.category,
                     "note": f.title} for f in forbidden
                ],
            }, sort_keys=False))
            written += 1
    return written


def _main() -> None:
    import argparse
    import os

    from reva.db.engine import create_engine_from_url

    parser = argparse.ArgumentParser(prog="python -m eval.bootstrap")
    parser.add_argument("--since", required=True)  # YYYY-MM-DD
    parser.add_argument("--repo", default=None)
    parser.add_argument("--golden-dir", default="eval/golden", type=Path)
    parser.add_argument("--min-findings", type=int, default=1)
    args = parser.parse_args()

    db = Database(create_engine_from_url(os.environ["DATABASE_URL"]))
    since = datetime.fromisoformat(args.since).replace(tzinfo=None) if "T" in args.since \
        else datetime.fromisoformat(args.since + "T00:00:00")
    n = bootstrap_drafts(db, args.golden_dir, since, repo=args.repo,
                         min_findings=args.min_findings)
    print(f"{n} draft golden entries written to {args.golden_dir} — curate, "
          f"remove 'draft: true', commit.")


if __name__ == "__main__":
    _main()
```

(Adapter notes: exact ORM field names — `ReviewFinding.file` may be
`file_path`, `PullRequest.pr_number` may be `number`, the outcome column name
per migration 015 — Task 0's grep output is authoritative; the tests' seeding
helper adapts with it. Timezone handling of `since` should match how
`created_at` is stored.)

- [ ] **Step 3: Run to verify pass, commit**

```bash
cd worker && .venv/bin/python -m pytest tests/test_eval_bootstrap.py -q
git add eval/bootstrap.py worker/tests/test_eval_bootstrap.py
git commit -m "feat(eval): bootstrap draft golden entries from the outcome ledger"
```

---

### Task 6: Docs + final verification

**Files:**
- Create: `eval/README.md`
- Modify: `CLAUDE.md` (one line in components), `worker/Dockerfile` (only if Task 0 found `eval/` missing from the image)

- [ ] **Step 1: Write `eval/README.md`**

```markdown
# Golden-PR eval harness

Replays curated PRs through the PURE review pipeline (`Reviewer.execute` —
nothing is posted to GitHub) and scores findings against version-controlled
expectations. Spec: `docs/superpowers/specs/2026-07-05-golden-pr-eval-design.md`.

Every replay is a PAID Claude call (~$0.10–0.25/PR): the runner prints an
estimate and requires `--yes`; spend lands in the ledger as `kind="eval"`.

## Workflow (inside the worker container)

    # one-time: mine draft entries from NEW-process reviews, then hand-curate
    python -m eval.bootstrap --since 2026-07-15
    #   → edit eval/golden/*.yml, remove `draft: true`, commit

    # baseline
    python -m eval.replay --label v1.9-baseline --yes
    python -m eval.report --label v1.9-baseline

    # A/B a change (new prompt version, model, engine flag)
    python -m eval.replay --label candidate --yes
    python -m eval.report --label candidate --compare v1.9-baseline

Golden entries: `repo`, `pr_number`, pinned `head_sha`, `expected` findings
(file + line ± tolerance + optional category) and `forbidden` known false
positives. `eval/results/` is gitignored except committed baseline reports.
```

Also add `eval/results/` to `.gitignore` (keep `eval/golden/` tracked).

- [ ] **Step 2: CLAUDE.md line**

In the Commands section, after the test commands, add:

```markdown
# Golden-PR eval (paid; see eval/README.md) — replay curated PRs, score vs expectations
docker compose exec worker python -m eval.replay --label <l> --yes && docker compose exec worker python -m eval.report --label <l>
```

- [ ] **Step 3: Full gate + commit**

```bash
cd worker && .venv/bin/python -m pytest tests/ -q
ruff check reva worker/worker api/app scheduler/scheduler eval
git add eval/README.md CLAUDE.md .gitignore worker/Dockerfile
git commit -m "docs(eval): harness README + CLAUDE.md command"
```

Final report must state: the paid replay path is untested until the first
real run (activation checklist in the spec); adapter notes applied from
Task 0 and what actually drifted.
