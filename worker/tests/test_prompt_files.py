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
    assert builder.get_version() == "v2.4"


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


def test_summary_contract_present():
    guidance = (PROMPTS_DIR / "review_guidance.md").read_text()
    assert "Summary contract" in guidance
    # every skill's summary placeholder points reviewers at the contract
    for skill in SKILLS_DIR.glob("reva-*.md"):
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
