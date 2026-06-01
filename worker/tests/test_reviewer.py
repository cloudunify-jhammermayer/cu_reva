"""Tests for Reviewer.execute.

Uses in-memory fakes for GitHubReader, RepoLookup, ClaudeClient, and
PromptBuilder so we never touch the network or the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from reva.errors import PermanentError, TransientError
from worker.reviewer import (
    Reviewer,
    _cap_findings,
    _recompute_risk_level,
)
from reva.types import ClaudeResponse, Finding, JobParams


# --- Fakes --------------------------------------------------------------------


_DEFAULT_DIFF = (
    "diff --git a/custom_addons/app.py b/custom_addons/app.py\n"
    "+++ b/custom_addons/app.py\n"
    "+ added\n"
    "- removed\n"
)

_DEFAULT_PR = {
    "pr_number": 42,
    "title": "Add foo",
    "body": "",
    "base_branch": "main",
    "head_branch": "feat/foo",
}


@dataclass
class FakeGitHub:
    diff: str = _DEFAULT_DIFF
    files: list[dict] = field(default_factory=lambda: [{"filename": "custom_addons/app.py"}])
    head_sha: str = "deadbeef"
    file_contents: dict[str, str | None] = field(default_factory=dict)
    diff_calls: int = 0
    token_calls: int = 0
    compare_diff: str = _DEFAULT_DIFF
    compare_diff_calls: int = 0

    def get_installation_token(self, installation_id: int) -> str:
        self.token_calls += 1
        return "ghs_tok"

    def get_pull_request(self, token, owner, repo, pr_number) -> dict:
        return {"head": {"sha": self.head_sha}, "body": "PR body from GitHub"}

    def get_pull_request_diff(self, token, owner, repo, pr_number) -> str:
        self.diff_calls += 1
        return self.diff

    def get_compare_diff(self, token, owner, repo, base_sha, head_sha) -> str:
        self.compare_diff_calls += 1
        return self.compare_diff

    def get_changed_files(self, token, owner, repo, pr_number) -> list[dict]:
        return self.files

    def get_file_content(self, token, owner, repo, path, ref) -> str | None:
        return self.file_contents.get(path)


@dataclass
class FakeRepos:
    owner: str = "acme"
    name: str = "widgets"
    pr: dict = field(
        default_factory=lambda: {
            "pr_number": 42,
            "title": "Add foo",
            "body": "",
            "base_branch": "main",
            "head_branch": "feat/foo",
        }
    )
    last_completed_review: dict | None = None

    def get_owner_name(self, repository_id: int) -> tuple[str, str]:
        return self.owner, self.name

    def get_pr_basic(self, pull_request_id: int) -> dict:
        return self.pr

    def get_last_completed_review(self, pull_request_id: int) -> dict | None:
        return self.last_completed_review


@dataclass
class FakeRunner:
    """Fake ClaudeCodeRunner for Reviewer tests."""
    response: ClaudeResponse | None = None
    raise_exc: Exception | None = None
    default_model: str = "claude-sonnet-4-6"
    deep_model: str = "claude-opus-4-7"
    last_model: str | None = None
    last_skill: str | None = None
    last_params: dict | None = None
    repo_path_returned: str = "/fake/repos/acme/widgets"

    def repo_lock(self, owner: str, name: str):
        import contextlib
        return contextlib.nullcontext()

    def ensure_repo(self, owner: str, name: str, head_sha: str | None, token: str) -> str:
        return self.repo_path_returned

    def review(self, repo_path: str, skill: str, params: dict, model: str | None = None) -> ClaudeResponse:
        self.last_model = model
        self.last_skill = skill
        self.last_params = params
        if self.raise_exc:
            raise self.raise_exc
        return self.response


class FakePrompts:
    """Stand-in for PromptBuilder that does no file IO."""

    def __init__(self, version: str = "v1.0") -> None:
        self.version = version

    def get_version(self) -> str:
        return self.version


def _claude_response_with_findings(findings: list[dict]) -> ClaudeResponse:
    return ClaudeResponse(
        model="claude-sonnet-4-6",
        stop_reason="tool_use",
        tool_use_input={
            "summary": "Looks fine overall.",
            "risk_level": "low",
            "findings": findings,
        },
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=2000,
        cache_creation_tokens=300,
    )


def _make_reviewer(**overrides):
    github = overrides.pop("github", None) or FakeGitHub()
    repos = overrides.pop("repos", None) or FakeRepos()
    runner = overrides.pop("runner", None) or FakeRunner()
    prompts = overrides.pop("prompts", None) or FakePrompts()
    reviewer = Reviewer(
        runner=runner,  # type: ignore[arg-type]
        github=github,
        repos=repos,
        prompts=prompts,  # type: ignore[arg-type]
        **overrides,
    )
    return reviewer, github, repos, runner, prompts


def _params(**overrides) -> JobParams:
    base = {
        "repository_id": 1,
        "pull_request_id": 1,
        "head_sha": "deadbeef",
        "installation_id": 100,
        "review_mode": "diff",
        "trigger_event": "opened",
    }
    base.update(overrides)
    return JobParams(**base)


# --- happy path ---------------------------------------------------------------


def test_happy_path_returns_completed_result():
    finding = {
        "severity": "minor",
        "category": "maintainability",
        "file": "custom_addons/app.py",
        "line_start": 10,
        "line_end": 10,
        "title": "Use clearer variable name",
        "body": "Detail here.",
        "suggestion": None,
        "confidence": 0.7,
        "is_odoo_specific": False,
    }
    runner = FakeRunner(response=_claude_response_with_findings([finding]))
    reviewer, gh, _, _, prompts = _make_reviewer(runner=runner)

    result = reviewer.execute(_params())

    assert result.status == "completed"
    assert result.summary == "Looks fine overall."
    assert len(result.findings) == 1
    assert result.findings[0].title == "Use clearer variable name"
    assert result.risk_level == "low"
    assert result.model == "claude-sonnet-4-6"
    assert result.prompt_version == "v1.0"
    assert result.input_tokens == 100
    assert result.cache_read_tokens == 2000
    assert result.cache_creation_tokens == 300
    assert result.estimated_cost_usd > 0
    assert result.duration_ms is not None and result.duration_ms >= 0
    assert result.diff == gh.diff
    assert gh.diff_calls == 1
    assert gh.token_calls == 1


def test_deep_mode_uses_opus_model():
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer, *_ = _make_reviewer(runner=runner)
    reviewer.execute(_params(review_mode="deep"))
    assert runner.last_model == "claude-opus-4-7"


def test_default_mode_uses_sonnet_model():
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer, *_ = _make_reviewer(runner=runner)
    reviewer.execute(_params(review_mode="diff"))
    assert runner.last_model == "claude-sonnet-4-6"


# --- finding grounding (A3) ---------------------------------------------------


def _finding(title: str, file: str | None, confidence: float = 0.9) -> dict:
    return {
        "severity": "major", "category": "bug", "file": file,
        "line_start": 1 if file else None, "line_end": 1 if file else None,
        "title": title, "body": "b", "suggestion": None,
        "confidence": confidence, "is_odoo_specific": False,
    }


def test_ungrounded_findings_dropped_when_clone_present(tmp_path):
    """Findings citing a file that doesn't exist in the clone (hallucinated or
    injection-fabricated, incl. path traversal) are dropped; real-file and
    general (no-file) findings are kept."""
    (tmp_path / "custom_addons").mkdir()
    (tmp_path / "custom_addons" / "real.py").write_text("x = 1\n")
    findings = [
        _finding("real", "custom_addons/real.py"),
        _finding("ghost", "custom_addons/nope.py"),
        _finding("escape", "../../etc/passwd"),
        _finding("general", None),
    ]
    runner = FakeRunner(
        response=_claude_response_with_findings(findings),
        repo_path_returned=str(tmp_path),
    )
    reviewer, *_ = _make_reviewer(runner=runner)

    titles = {f.title for f in reviewer.execute(_params()).findings}
    assert titles == {"real", "general"}


def test_findings_not_dropped_when_clone_absent():
    """Fail-open: if the clone path isn't present we can't verify, so keep
    findings rather than nuking all of them."""
    runner = FakeRunner(
        response=_claude_response_with_findings([_finding("keep-me", "any/where.py")]),
        repo_path_returned="/fake/does/not/exist",
    )
    reviewer, *_ = _make_reviewer(runner=runner)
    assert any(f.title == "keep-me" for f in reviewer.execute(_params()).findings)


# --- stale --------------------------------------------------------------------


def test_stale_head_returns_stale_without_fetching_diff():
    github = FakeGitHub(head_sha="newsha")  # current SHA differs from job
    reviewer, gh, *_ = _make_reviewer(github=github)
    result = reviewer.execute(_params(head_sha="deadbeef"))
    assert result.status == "stale"
    assert gh.diff_calls == 0


# --- declined paths -----------------------------------------------------------


def test_decline_when_diff_too_many_lines():
    big_diff = "\n".join(f"+line {i}" for i in range(2501))
    github = FakeGitHub(diff=big_diff)
    reviewer, *_ = _make_reviewer(github=github)
    result = reviewer.execute(_params())
    assert result.status == "declined"
    assert "lines" in (result.decline_reason or "").lower()


def test_decline_when_diff_exceeds_token_budget():
    # Many short '+' lines so line count stays under default 1000 but
    # token estimate (chars/4) exceeds the configured 200 cap.
    diff = "\n".join(f"+x{i:08d}" for i in range(500))  # ~5000 chars
    github = FakeGitHub(diff=diff)
    reviewer, *_ = _make_reviewer(github=github, max_diff_tokens=200)
    result = reviewer.execute(_params())
    assert result.status == "declined"
    assert "token" in (result.decline_reason or "").lower()


def test_per_repo_config_tightens_max_diff_lines():
    diff = "\n".join(f"+line {i}" for i in range(150))  # 150 lines
    github = FakeGitHub(
        diff=diff,
        file_contents={".claude-review.yml": "max_diff_lines: 100\n"},
    )
    reviewer, *_ = _make_reviewer(github=github)  # default cap is 2500
    result = reviewer.execute(_params())
    assert result.status == "declined"
    assert "100" in (result.decline_reason or "")


def test_decline_when_all_files_match_skip_paths():
    # Use a diff under custom_addons/ so it passes filter_diff, then gets stripped by skip_paths.
    github = FakeGitHub(
        diff=(
            "diff --git a/custom_addons/package-lock.json b/custom_addons/package-lock.json\n"
            "+++ b/custom_addons/package-lock.json\n"
            "+ lock content\n"
        ),
        files=[{"filename": "custom_addons/package-lock.json"}],
        file_contents={".claude-review.yml": "skip_paths:\n  - '*.json'\n"},
    )
    reviewer, *_ = _make_reviewer(github=github)
    result = reviewer.execute(_params())
    assert result.status == "declined"
    assert "skip_paths" in (result.decline_reason or "")


# --- repo config edge cases ---------------------------------------------------


def test_malformed_claude_review_yml_falls_back_to_empty():
    github = FakeGitHub(
        file_contents={".claude-review.yml": "max_diff_lines: [unclosed"},
    )
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer, *_ = _make_reviewer(github=github, runner=runner)
    result = reviewer.execute(_params())
    # Did NOT decline — default limits applied since the YAML was unparseable.
    assert result.status == "completed"


def test_custom_instructions_appended_as_block():
    # custom_instructions in .claude-review.yml are loaded via _load_repo_config.
    # Claude Code reads CLAUDE.md from the cloned repo directly, so build_system_blocks
    # is no longer called. The review must still complete successfully.
    github = FakeGitHub(
        file_contents={
            ".claude-review.yml": (
                "custom_instructions: |\n"
                "  This module handles money. Be strict about currency_id.\n"
            )
        }
    )
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer, _, _, runner_out, _ = _make_reviewer(github=github, runner=runner)
    result = reviewer.execute(_params())
    assert result.status == "completed"


def test_missing_claude_md_and_yml_still_succeeds():
    github = FakeGitHub(file_contents={})  # nothing fetched
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer, *_ = _make_reviewer(github=github, runner=runner)
    result = reviewer.execute(_params())
    assert result.status == "completed"


# --- finding cap + risk recompute --------------------------------------------


def test_findings_capped_to_15_by_severity_and_confidence():
    # 17 minor findings + 1 critical buried at the end. Critical must survive.
    findings = [
        {
            "severity": "minor",
            "category": "maintainability",
            "title": f"Minor #{i}",
            "body": "x",
            "confidence": 0.9,
            "is_odoo_specific": False,
        }
        for i in range(17)
    ]
    findings.append(
        {
            "severity": "critical",
            "category": "security",
            "title": "SQL injection",
            "body": "x",
            "confidence": 0.5,
            "is_odoo_specific": False,
        }
    )
    runner = FakeRunner(response=_claude_response_with_findings(findings))
    reviewer, *_ = _make_reviewer(runner=runner)
    result = reviewer.execute(_params())
    assert len(result.findings) == 15
    assert any(f.severity == "critical" for f in result.findings)
    # risk recomputed deterministically — critical present -> critical
    assert result.risk_level == "critical"


def test_risk_level_recomputed_when_no_critical_or_major():
    findings = [
        {
            "severity": "minor",
            "category": "style",
            "title": f"m {i}",
            "body": "x",
            "confidence": 0.6,
            "is_odoo_specific": False,
        }
        for i in range(4)
    ]
    runner = FakeRunner(response=_claude_response_with_findings(findings))
    reviewer, *_ = _make_reviewer(runner=runner)
    result = reviewer.execute(_params())
    # >= 3 minor -> medium
    assert result.risk_level == "medium"


# --- error propagation --------------------------------------------------------


def test_invalid_finding_shape_raises_permanent_error():
    bad = {
        "severity": "blocker",  # not in Literal
        "category": "bug",
        "title": "x",
        "body": "x",
        "confidence": 0.5,
        "is_odoo_specific": False,
    }
    runner = FakeRunner(response=_claude_response_with_findings([bad]))
    reviewer, *_ = _make_reviewer(runner=runner)
    with pytest.raises(PermanentError):
        reviewer.execute(_params())


def test_missing_summary_raises_permanent_error():
    runner = FakeRunner(
        response=ClaudeResponse(
            model="claude-sonnet-4-6",
            stop_reason="tool_use",
            tool_use_input={"summary": "", "risk_level": "low", "findings": []},
            input_tokens=1,
            output_tokens=1,
        )
    )
    reviewer, *_ = _make_reviewer(runner=runner)
    with pytest.raises(PermanentError):
        reviewer.execute(_params())


def test_transient_error_propagates():
    runner = FakeRunner(raise_exc=TransientError("rate limited", retry_after=30))
    reviewer, *_ = _make_reviewer(runner=runner)
    with pytest.raises(TransientError):
        reviewer.execute(_params())


# --- pure helper tests --------------------------------------------------------


def _f(severity: str, confidence: float, title: str = "t") -> Finding:
    return Finding(
        severity=severity,  # type: ignore[arg-type]
        category="bug",
        title=title,
        body="x",
        confidence=confidence,
        is_odoo_specific=False,
    )


def test_cap_findings_ranks_by_severity_times_confidence():
    # critical * 0.5 = 2.0 beats minor * 0.9 = 1.8 — high-severity wins
    # at comparable confidence, per pr-review-requirements §5 rule 10.
    high = _f("critical", 0.5, "keep-critical")
    middling = [_f("minor", 0.9, f"minor-{i}") for i in range(20)]
    capped = _cap_findings([*middling, high], 15)
    assert any(f.title == "keep-critical" for f in capped)


def test_cap_findings_drops_low_confidence_high_severity_per_spec():
    # Spec rule: severity * confidence determines rank. A 0.3-confidence
    # critical (1.2) SHOULD lose to a 1.0-confidence minor (2.0). This
    # documents the trade-off so anyone changing the spec sees the test.
    low_conf_critical = _f("critical", 0.3, "low-conf-critical")
    high_conf_minors = [_f("minor", 1.0, f"m{i}") for i in range(20)]
    capped = _cap_findings([low_conf_critical, *high_conf_minors], 15)
    assert all(f.title != "low-conf-critical" for f in capped)


def test_cap_findings_noop_when_under_limit():
    findings = [_f("minor", 1.0)] * 5
    assert _cap_findings(findings, 15) is findings


def test_recompute_risk_level_levels():
    assert _recompute_risk_level([_f("critical", 0.5)]) == "critical"
    assert _recompute_risk_level([_f("major", 0.5)]) == "high"
    assert _recompute_risk_level([_f("minor", 0.5)] * 3) == "medium"
    assert _recompute_risk_level([_f("minor", 0.5)]) == "low"
    assert _recompute_risk_level([]) == "low"


# --- delta detection ----------------------------------------------------------


def test_delta_review_used_when_prior_review_exists():
    """When a completed review exists, get_compare_diff is called and reva-delta-review skill used."""
    github = FakeGitHub(head_sha="newsha", compare_diff=_DEFAULT_DIFF)
    repos = FakeRepos(pr=_DEFAULT_PR, last_completed_review={"id": 1, "head_sha": "prevsha"})
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer, *_ = _make_reviewer(github=github, repos=repos, runner=runner)
    params = JobParams(
        repository_id=1, pull_request_id=1, head_sha="newsha",
        installation_id=99, trigger_event="synchronize",
    )

    result = reviewer.execute(params)

    assert result.status == "completed"
    assert result.delta_base_sha == "prevsha"
    assert runner.last_skill == "reva-delta-review"
    assert github.compare_diff_calls == 1


def test_full_review_used_when_no_prior_review():
    """Without a prior review, get_pull_request_diff is called and reva-diff-review skill used."""
    github = FakeGitHub(head_sha="sha1")
    repos = FakeRepos(pr=_DEFAULT_PR, last_completed_review=None)
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer, *_ = _make_reviewer(github=github, repos=repos, runner=runner)
    params = JobParams(
        repository_id=1, pull_request_id=1, head_sha="sha1",
        installation_id=99, trigger_event="synchronize",
    )

    result = reviewer.execute(params)

    assert result.status == "completed"
    assert result.delta_base_sha is None
    assert runner.last_skill in ("reva-diff-review", "reva-full-review")
    assert github.diff_calls == 1


def test_delta_empty_returns_stale():
    """If the compare diff is empty, return stale without calling Claude."""
    github = FakeGitHub(head_sha="newsha", compare_diff="")
    repos = FakeRepos(pr=_DEFAULT_PR, last_completed_review={"id": 1, "head_sha": "prevsha"})
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer, *_ = _make_reviewer(github=github, repos=repos, runner=runner)
    params = JobParams(
        repository_id=1, pull_request_id=1, head_sha="newsha",
        installation_id=99, trigger_event="synchronize",
    )

    result = reviewer.execute(params)

    assert result.status == "stale"
    assert runner.last_skill is None  # Claude never called


# --- /review-all (diff-all: diff depth, all paths) ----------------------------

_OUTSIDE_DIFF = (
    "diff --git a/scripts/deploy.py b/scripts/deploy.py\n"
    "+++ b/scripts/deploy.py\n"
    "+ added\n"
    "- removed\n"
)


def _outside_github():
    return FakeGitHub(diff=_OUTSIDE_DIFF, files=[{"filename": "scripts/deploy.py"}])


def test_diff_mode_declines_changes_outside_custom_addons():
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer, *_ = _make_reviewer(github=_outside_github(), runner=runner)
    result = reviewer.execute(_params(review_mode="diff"))
    assert result.status == "declined"  # custom_addons prefix filter drops it


def test_review_all_reviews_changes_outside_custom_addons():
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer, *_ = _make_reviewer(github=_outside_github(), runner=runner)
    result = reviewer.execute(_params(review_mode="diff-all"))
    assert result.status == "completed"
    assert "scripts/deploy.py" in runner.last_params["diff"]
    assert "scripts/deploy.py" in runner.last_params["changed_files"]


def test_review_all_uses_diff_skill_not_full():
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer, *_ = _make_reviewer(github=_outside_github(), runner=runner)
    reviewer.execute(_params(review_mode="diff-all"))
    assert runner.last_skill == "reva-diff-review"


def test_review_all_uses_default_model():
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer, *_ = _make_reviewer(github=_outside_github(), runner=runner)
    reviewer.execute(_params(review_mode="diff-all"))
    assert runner.last_model == "claude-sonnet-4-6"


def test_diff_under_2500_line_cap_is_reviewed():
    # 2400 added lines under custom_addons — over the old 2000 cap, under the
    # current 2500 default. Should be reviewed, not declined.
    body = "\n".join(f"+line {i}" for i in range(2400))
    diff = (
        "diff --git a/custom_addons/big.py b/custom_addons/big.py\n"
        "+++ b/custom_addons/big.py\n"
        f"{body}\n"
    )
    github = FakeGitHub(diff=diff, files=[{"filename": "custom_addons/big.py"}])
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer, *_ = _make_reviewer(github=github, runner=runner)
    result = reviewer.execute(_params())
    assert result.status == "completed"
