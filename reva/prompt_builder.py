"""Prompt versioning and drift-detection for the prompts/ directory.

The CLI review path assembles its prompt directly in
`reva.claude_code_runner` (review_guidance.md + odoo19.md + skills/*.md); this
module owns only the version string (parsed from CHANGELOG.md) and the content
hashes the drift guard compares on boot. The earlier Messages-API assembly
methods (system.md / *_review.md) were removed once the CLI became the only
review path.
"""

from __future__ import annotations

import hashlib
import os


class PromptBuilder:
    def __init__(self, prompts_dir: str = "/app/prompts") -> None:
        self.prompts_dir = prompts_dir

    # --- Versioning ----------------------------------------------------------

    def get_version(self) -> str:
        """Return prompt version parsed from the first ## heading in CHANGELOG.md."""
        changelog = self._read("CHANGELOG.md")
        for line in changelog.strip().split("\n"):
            line = line.strip()
            if line.startswith("##"):
                heading = line.lstrip("#").strip()
                # Strip the trailing description after the separator: em dash (—),
                # en dash (–), or a spaced ASCII hyphen ( - ). The spaced form
                # avoids splitting a hyphenated version like "v1.2-beta" (MAIN-12).
                return heading.split("—")[0].split("–")[0].split(" - ")[0].strip()
        raise ValueError("No ## heading found in CHANGELOG.md")

    def compute_prompt_hashes(self, skills_dir: str) -> tuple[str, str]:
        """Content hashes of the prompt files the CLI review path actually
        assembles (see ClaudeCodeRunner._build_preamble / _read_skill).

        Returns (system_prompt_hash, review_prompt_hash):
          - system_prompt_hash = sha256(review_guidance.md), the always-on
            governance preamble.
          - review_prompt_hash = sha256 over odoo19.md plus every skills/*.md,
            sorted by filename with each chunk prefixed by its name so a rename
            or reorder also changes the hash.

        The column names are inherited from an earlier Messages-API design (a
        system.md / *_review.md split) and do NOT reflect the current CLI
        pipeline — the CLI never reads system.md. A missing required file raises
        FileNotFoundError: callers must NOT record a hash of an empty string,
        which would mask a deleted file as 'unchanged'.
        """
        system_hash = hashlib.sha256(self._read("review_guidance.md").encode()).hexdigest()

        h = hashlib.sha256()
        h.update(b"odoo19.md\n")
        h.update(self._read("odoo19.md").encode())
        for fname in sorted(os.listdir(skills_dir)):
            if not fname.endswith(".md"):
                continue
            with open(os.path.join(skills_dir, fname)) as f:
                content = f.read()
            h.update(f"\n{fname}\n".encode())
            h.update(content.encode())
        return system_hash, h.hexdigest()

    # --- IO -----------------------------------------------------------------

    def _read(self, filename: str) -> str:
        path = os.path.join(self.prompts_dir, filename)
        with open(path) as f:
            return f.read()
