"""Assemble prompts from the prompts/ directory.

The system prompt is returned as a LIST of content blocks so each cacheable
chunk (system.md, odoo19.md, CLAUDE.md) can carry its own cache_control
marker. The Claude Messages API caches at block boundaries.

Cache strategy (see doc 07):
- Block 1: system.md                              — cached (changes per prompt version).
- Block 2: odoo19.md (if Odoo repo)               — cached (changes per prompt version).
- Block 3: CLAUDE.md (if repo has one)            — cached per repo head_sha-of-file.
- Block 4: custom_instructions from repo config   — cached per repo head_sha-of-file.
- User message (PR diff + metadata)               — NOT cached (changes every PR).
"""

from __future__ import annotations

import os

from reva.types import ContentBlock, RepoConfig, ReviewMode

_EPHEMERAL_CACHE: dict = {"type": "ephemeral"}


class PromptBuilder:
    def __init__(self, prompts_dir: str = "/app/prompts") -> None:
        self.prompts_dir = prompts_dir

    # --- System prompt assembly ----------------------------------------------

    def build_system_blocks(
        self,
        repo_config: RepoConfig,
        claude_md: str | None,
    ) -> list[ContentBlock]:
        """Return cache-tagged content blocks for the Claude `system` field."""
        blocks: list[ContentBlock] = []

        base = self._read("system.md")
        blocks.append({"type": "text", "text": base, "cache_control": _EPHEMERAL_CACHE})

        if repo_config.odoo or repo_config.framework == "odoo":
            odoo = self._read("odoo19.md")
            blocks.append({"type": "text", "text": odoo, "cache_control": _EPHEMERAL_CACHE})

        if claude_md:
            blocks.append(
                {
                    "type": "text",
                    "text": f"## Repository-Specific Instructions\n\n{claude_md}",
                    "cache_control": _EPHEMERAL_CACHE,
                }
            )

        if repo_config.custom_instructions and repo_config.custom_instructions.strip():
            blocks.append(
                {
                    "type": "text",
                    "text": f"## Custom Review Instructions\n\n{repo_config.custom_instructions.strip()}",
                    "cache_control": _EPHEMERAL_CACHE,
                }
            )

        return blocks

    # --- User prompt assembly ------------------------------------------------

    def build_user_prompt(
        self,
        mode: ReviewMode,
        pr_title: str,
        pr_body: str,
        diff: str,
        changed_files: list[str],
        base_branch: str,
        head_branch: str,
    ) -> str:
        template = self._read(f"{mode}_review.md")
        return template.format(
            pr_title=pr_title,
            pr_body=pr_body or "(no description)",
            diff=diff,
            changed_files="\n".join(f"- {f}" for f in changed_files),
            base_branch=base_branch,
            head_branch=head_branch,
        )

    # --- Versioning ----------------------------------------------------------

    def get_version(self) -> str:
        """Return prompt version parsed from the first heading in CHANGELOG.md."""
        changelog = self._read("CHANGELOG.md")
        first_line = changelog.strip().split("\n")[0]
        return first_line.split("—")[0].replace("##", "").strip()

    # --- IO -----------------------------------------------------------------

    def _read(self, filename: str) -> str:
        path = os.path.join(self.prompts_dir, filename)
        with open(path) as f:
            return f.read()
