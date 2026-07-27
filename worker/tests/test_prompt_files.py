"""Content sanity tests for the on-disk prompts/ directory.

Verifies that PromptBuilder can load the real files and that the major
template placeholders survive the read/format cycle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reva.prompt_builder import PromptBuilder
from worker.reviewer import _ODOO_SEVERITY_RULES

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
SKILLS_DIR = PROMPTS_DIR / "skills"

# Each deterministic calibration rule must trace to a phrase that actually
# appears in odoo19.md, so the rule table and the prompt can't silently drift.
_RULE_ANCHORS = {
    "cr_execute_string_format": "cr.execute()",
    "manual_transaction": "cr.commit()",
    "missing_model_access": "ir.model.access.csv",
    "sudo_in_controller": "sudo()",
    "controller_auth_none": "auth='none'",
    "api_depends_missing": "@api.depends",
    "api_onchange_writes_db": "@api.onchange",
    "csp_inline_script": "inline `<script>`",
    "manifest_missing_depends": "__manifest__.py",
    "missing_record_rule": "ir.rule",
}


@pytest.fixture()
def builder() -> PromptBuilder:
    return PromptBuilder(prompts_dir=str(PROMPTS_DIR))


def test_prompts_dir_exists():
    assert PROMPTS_DIR.is_dir(), f"prompts/ missing at {PROMPTS_DIR}"
    for name in ("review_guidance.md", "odoo19.md", "CHANGELOG.md"):
        assert (PROMPTS_DIR / name).is_file(), f"prompts/{name} missing"


def test_get_version_returns_current_version(builder):
    assert builder.get_version() == "v2.12"


def test_ticket_issue_type_is_ticket_level():
    # v2.9: the work-item type is classified once from the ticket's overall
    # nature and inherited by every issue (not per-issue dominant purpose).
    text = (PROMPTS_DIR / "ticket_issues.md").read_text()
    assert "Classify the ticket once" in text


def test_odoo_severity_rules_in_sync_with_odoo19_md():
    text = (PROMPTS_DIR / "odoo19.md").read_text().lower()
    rule_names = {name for name, _, _ in _ODOO_SEVERITY_RULES}
    assert rule_names == set(_RULE_ANCHORS), (
        "every calibration rule needs an anchor phrase in _RULE_ANCHORS (and vice versa)"
    )
    for name, anchor in _RULE_ANCHORS.items():
        assert anchor.lower() in text, f"rule {name!r} anchor {anchor!r} missing from odoo19.md"


# --- compute_prompt_hashes (prompt-version drift detection) ------------------


def test_compute_prompt_hashes_real_dir_is_deterministic(builder):
    h1 = builder.compute_prompt_hashes(str(SKILLS_DIR))
    h2 = builder.compute_prompt_hashes(str(SKILLS_DIR))
    assert h1 == h2
    system_hash, review_hash = h1
    assert len(system_hash) == 64 and all(c in "0123456789abcdef" for c in system_hash)
    assert len(review_hash) == 64 and system_hash != review_hash


def _seed_prompts(tmp_path):
    (tmp_path / "review_guidance.md").write_text("governance v1\n")
    (tmp_path / "odoo19.md").write_text("odoo rules v1\n")
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "reva-diff-review.md").write_text("diff skill v1\n")
    return PromptBuilder(prompts_dir=str(tmp_path)), str(skills)


def test_changing_review_guidance_flips_only_system_hash(tmp_path):
    pb, skills = _seed_prompts(tmp_path)
    sys0, rev0 = pb.compute_prompt_hashes(skills)
    (tmp_path / "review_guidance.md").write_text("governance v2 — edited\n")
    sys1, rev1 = pb.compute_prompt_hashes(skills)
    assert sys1 != sys0
    assert rev1 == rev0


def test_changing_a_skill_flips_only_review_hash(tmp_path):
    pb, skills = _seed_prompts(tmp_path)
    sys0, rev0 = pb.compute_prompt_hashes(skills)
    (Path(skills) / "reva-diff-review.md").write_text("diff skill v2 — edited\n")
    sys1, rev1 = pb.compute_prompt_hashes(skills)
    assert rev1 != rev0
    assert sys1 == sys0


def test_compute_prompt_hashes_missing_file_raises(tmp_path):
    # A missing required file must raise (never silently hash an empty string).
    (tmp_path / "skills").mkdir()
    pb = PromptBuilder(prompts_dir=str(tmp_path))  # no review_guidance.md
    with pytest.raises(FileNotFoundError):
        pb.compute_prompt_hashes(str(tmp_path / "skills"))


def test_test_coverage_guidance_present():
    text = (PROMPTS_DIR / "review_guidance.md").read_text()
    assert "test_coverage" in text
    for skill in ("reva-diff-review.md", "reva-delta-review.md", "reva-full-review.md"):
        assert "test_coverage" in (SKILLS_DIR / skill).read_text(), skill


def test_stated_intent_guidance_present():
    assert "stated_intent" in (PROMPTS_DIR / "review_guidance.md").read_text()


def test_intent_check_guidance_present():
    guidance = (PROMPTS_DIR / "review_guidance.md").read_text()
    assert "intent_check" in guidance
    for verdict in ("matches", "partial", "does_not_match", "unclear"):
        assert verdict in guidance, verdict
    # Delta scoping must be stated: no verdicts on delta reviews.
    assert "delta" in guidance


def test_intent_check_in_skill_output_contracts():
    # The live headless-CLI output contract is each skill's "Output format", not
    # review_guidance.md — so the four full-diff skills must carry intent_check
    # there or the model may follow the template and never emit verdicts. The
    # delta skill deliberately omits it (delta verdicts are dropped at parse).
    for skill in (
        "reva-diff-review.md", "reva-full-review.md",
        "reva-xml-review.md", "reva-migration-review.md",
    ):
        assert "intent_check" in (SKILLS_DIR / skill).read_text(), skill
    assert "intent_check" not in (SKILLS_DIR / "reva-delta-review.md").read_text()


def test_summary_contract_present():
    from reva.claude_code_runner import _NO_REVIEW_GUIDANCE_SKILLS

    guidance = (PROMPTS_DIR / "review_guidance.md").read_text()
    assert "Summary contract" in guidance
    # Every FINDINGS skill's summary placeholder points reviewers at the
    # contract. Skills that emit a different artifact (a support-answer draft,
    # a business analysis) never receive review_guidance.md at all, so
    # requiring the reference would be requiring a dangling pointer.
    for skill in SKILLS_DIR.glob("reva-*.md"):
        if skill.stem in _NO_REVIEW_GUIDANCE_SKILLS:
            continue
        assert "Summary contract" in skill.read_text(), skill.name


def test_verify_before_write_guidance_present():
    # The pre-output verification pass lives in the guidance; the confidence
    # threshold is stated once there (honestly), not repeated as a hard "≥ 0.7"
    # instruction in each skill that trains the model to inflate to the boundary.
    guidance = (PROMPTS_DIR / "review_guidance.md").read_text()
    assert "Verify before you write" in guidance
    for skill in SKILLS_DIR.glob("*.md"):
        assert "confidence ≥ 0.7" not in skill.read_text(), skill.name


def test_team_configuration_block_single_sourced():
    # The team-configuration handling lives once in review_guidance.md (prepended
    # to every skill), not copy-pasted into each skill where it would drift.
    guidance = (PROMPTS_DIR / "review_guidance.md").read_text()
    assert "team_review_preferences" in guidance
    assert "If a `custom_instructions` parameter is present" in guidance
    for skill in SKILLS_DIR.glob("*.md"):
        assert "If a `custom_instructions` parameter is present" not in skill.read_text(), (
            f"{skill.name} still carries the team-configuration block — it belongs "
            "only in review_guidance.md"
        )


def test_migration_skill_present_and_well_formed():
    text = (SKILLS_DIR / "reva-migration-review.md").read_text()
    assert text.strip(), "reva-migration-review.md is empty"
    assert "output_path" in text          # same submit_review output contract
    assert "risk_level" in text           # instructs NOT to set risk_level
    assert "migrate.py" in text or "migration" in text.lower()


def test_xml_skill_present_and_has_csp_anchor():
    text = (SKILLS_DIR / "reva-xml-review.md").read_text()
    assert text.strip(), "reva-xml-review.md is empty"
    assert "output_path" in text and "risk_level" in text
    # keep the CSP anchor so _ODOO_SEVERITY_RULES.csp_inline_script still floors it
    assert "<script" in text


def test_repo_aware_skills_have_security_consistency_step():
    # Guards against silent removal of the model->security cross-check guidance.
    for skill in ("reva-full-review.md", "reva-delta-review.md", "reva-repo-audit.md"):
        text = (SKILLS_DIR / skill).read_text()
        assert "ir.model.access.csv" in text, skill
        assert "_name =" in text, skill


def test_diff_skill_has_bounded_security_check():
    text = (SKILLS_DIR / "reva-diff-review.md").read_text()
    assert "ir.model.access.csv" in text
    assert "Only when" in text or "do not" in text.lower()  # the cheap path stays cheap


def test_manifest_guidance_present():
    # diff/delta/full receive the deterministic manifest_audit param
    for skill in ("reva-diff-review.md", "reva-delta-review.md", "reva-full-review.md"):
        assert "manifest_audit" in (SKILLS_DIR / skill).read_text(), skill
    # audit derives the manifest checks itself (no param)
    assert "__manifest__.py" in (SKILLS_DIR / "reva-repo-audit.md").read_text()
    odoo = (PROMPTS_DIR / "odoo19.md").read_text()
    assert "exists in the module" in odoo            # missing-file check
    assert "security before views" in odoo            # order check


def test_scanner_alert_guidance_present():
    guidance = (PROMPTS_DIR / "review_guidance.md").read_text()
    assert "Security-alert hints" in guidance
    assert "scanner_alerts" in guidance


def test_ticket_loop_prompt_guidance_present():
    guidance = (PROMPTS_DIR / "review_guidance.md").read_text()
    assert "ticket_acceptance_criteria" in guidance
    assert (PROMPTS_DIR / "change_note.md").is_file()


def test_support_answer_skill_exists_and_is_codegraph_enabled():
    """The support-answer skill is repo-aware — it explores a whole clone to
    trace where behaviour comes from — so it belongs in the CodeGraph
    allowlist alongside full review and audit."""
    from reva.claude_code_runner import _CODEGRAPH_SKILLS

    assert (SKILLS_DIR / "reva-support-answer.md").is_file()
    assert "reva-support-answer" in _CODEGRAPH_SKILLS


def test_support_answer_skill_states_the_load_bearing_rules():
    """These three are product safety properties, not prose: never reveal
    internal notes, no caveated draft on cannot_answer, cite what you read."""
    body = (SKILLS_DIR / "reva-support-answer.md").read_text().lower()
    assert "internal" in body and "never quote" in body
    assert "cannot_answer" in body
    assert "sources" in body


def test_non_review_skills_get_odoo_rules_but_not_findings_governance(tmp_path):
    """review_guidance.md defines severity/category/confidence and the findings
    summary contract. A support answer emits none of those, so prepending it is
    ~3k wasted tokens AND instructions that contradict the skill's own output
    contract. odoo19.md still applies — the domain doesn't change."""
    from reva.claude_code_runner import ClaudeCodeRunner

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "review_guidance.md").write_text("FINDINGS GOVERNANCE\n")
    (prompts / "odoo19.md").write_text("ODOO RULES\n")
    runner = ClaudeCodeRunner(
        repo_cache_dir=str(tmp_path), api_key="k",
        skills_dir=str(tmp_path), prompts_dir=str(prompts),
    )

    review = runner._build_preamble(odoo=True, skill="reva-full-review")
    assert "FINDINGS GOVERNANCE" in review and "ODOO RULES" in review

    support = runner._build_preamble(odoo=True, skill="reva-support-answer")
    assert "FINDINGS GOVERNANCE" not in support
    assert "ODOO RULES" in support


def test_both_support_prompts_agree_answer_is_plain_text():
    """The Messages-API prompt and the CLI skill both feed one formatter that
    ESCAPES answer. If either tells the model to emit HTML, the consultant
    sees literal tags. Two producers, one contract — pin it in both."""
    for path in (PROMPTS_DIR / "support_answer.md",
                 SKILLS_DIR / "reva-support-answer.md"):
        # Collapse wrapping — these are prose files; asserting on line breaks
        # would fail the next time someone reflows a paragraph.
        body = " ".join(path.read_text().split()).lower()
        assert "plain text" in body, path.name
        assert "do not emit html tags" in body, path.name


def test_support_prompts_forbid_caveated_draft_on_cannot_answer():
    """The no-hedged-draft rule is a product decision, not phrasing: a draft a
    consultant must fact-check from scratch costs more than no draft."""
    for path in (PROMPTS_DIR / "support_answer.md",
                 SKILLS_DIR / "reva-support-answer.md"):
        body = path.read_text().lower()
        assert "cannot_answer" in body, path.name
        assert "caveated" in body or "hedged" in body, path.name
