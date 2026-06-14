"""Content sanity tests for the on-disk prompts/ directory.

Verifies that PromptBuilder can load the real files and that the major
template placeholders survive the read/format cycle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reva.prompt_builder import PromptBuilder
from reva.types import RepoConfig
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
}


@pytest.fixture()
def builder() -> PromptBuilder:
    return PromptBuilder(prompts_dir=str(PROMPTS_DIR))


def test_prompts_dir_exists():
    assert PROMPTS_DIR.is_dir(), f"prompts/ missing at {PROMPTS_DIR}"
    for name in ("system.md", "diff_review.md", "deep_review.md", "odoo19.md", "CHANGELOG.md"):
        assert (PROMPTS_DIR / name).is_file(), f"prompts/{name} missing"


def test_system_blocks_baseline(builder):
    blocks = builder.build_system_blocks(RepoConfig(), claude_md=None)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert "REVA" in blocks[0]["text"]
    assert "submit_review" in blocks[0]["text"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_system_blocks_with_odoo(builder):
    blocks = builder.build_system_blocks(RepoConfig(framework="odoo"), claude_md=None)
    assert len(blocks) == 2
    assert "Odoo 19" in blocks[1]["text"]
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}


def test_system_blocks_with_claude_md(builder):
    blocks = builder.build_system_blocks(RepoConfig(), claude_md="This module handles money.")
    assert len(blocks) == 2
    assert "Repository-Specific Instructions" in blocks[1]["text"]
    assert "handles money" in blocks[1]["text"]


def test_system_blocks_full_set(builder):
    blocks = builder.build_system_blocks(
        RepoConfig(framework="odoo", custom_instructions="Be strict on currency_id."),
        claude_md="CLAUDE.md content here.",
    )
    # system + odoo + claude_md + custom = 4
    assert len(blocks) == 4
    texts = [b["text"] for b in blocks]
    assert any("REVA" in t for t in texts)
    assert any("Odoo 19" in t for t in texts)
    assert any("Repository-Specific Instructions" in t for t in texts)
    assert any("currency_id" in t for t in texts)


def test_get_version_returns_current_version(builder):
    assert builder.get_version() == "v1.6"


def test_odoo_severity_rules_in_sync_with_odoo19_md():
    text = (PROMPTS_DIR / "odoo19.md").read_text().lower()
    rule_names = {name for name, _, _ in _ODOO_SEVERITY_RULES}
    assert rule_names == set(_RULE_ANCHORS), (
        "every calibration rule needs an anchor phrase in _RULE_ANCHORS (and vice versa)"
    )
    for name, anchor in _RULE_ANCHORS.items():
        assert anchor.lower() in text, f"rule {name!r} anchor {anchor!r} missing from odoo19.md"


def test_diff_review_template_renders(builder):
    out = builder.build_user_prompt(
        mode="diff",
        pr_title="Add foo",
        pr_body="Implements feature X.",
        diff="@@ -1 +1 @@\n-old\n+new",
        changed_files=["x.py", "y.py"],
        base_branch="main",
        head_branch="feat/foo",
    )
    assert "Add foo" in out
    assert "Implements feature X" in out
    assert "@@ -1 +1 @@" in out
    assert "- x.py" in out
    assert "- y.py" in out
    assert "main" in out and "feat/foo" in out
    assert "submit_review" in out


def test_deep_review_template_adds_deep_sections(builder):
    out = builder.build_user_prompt(
        mode="deep",
        pr_title="Refactor auth",
        pr_body="",
        diff="@@ -1 +1 @@\n-old\n+new",
        changed_files=["auth.py"],
        base_branch="main",
        head_branch="refactor/auth",
    )
    assert "deep review" in out.lower()
    assert "Architectural impact" in out or "architectural" in out.lower()
    assert "Cross-file" in out or "cross-file" in out.lower()


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


def test_migration_skill_present_and_well_formed():
    text = (SKILLS_DIR / "reva-migration-review.md").read_text()
    assert text.strip(), "reva-migration-review.md is empty"
    assert "output_path" in text          # same submit_review output contract
    assert "risk_level" in text           # instructs NOT to set risk_level
    assert "migrate.py" in text or "migration" in text.lower()


def test_manifest_guidance_present():
    # diff/delta/full receive the deterministic manifest_audit param
    for skill in ("reva-diff-review.md", "reva-delta-review.md", "reva-full-review.md"):
        assert "manifest_audit" in (SKILLS_DIR / skill).read_text(), skill
    # audit derives the manifest checks itself (no param)
    assert "__manifest__.py" in (SKILLS_DIR / "reva-repo-audit.md").read_text()
    odoo = (PROMPTS_DIR / "odoo19.md").read_text()
    assert "exists in the module" in odoo            # missing-file check
    assert "security before views" in odoo            # order check
