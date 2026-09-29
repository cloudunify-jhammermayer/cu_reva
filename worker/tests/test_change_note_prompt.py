"""The user prompt of the merge change note (spec 2026-09-29)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from reva.change_note import build_note

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
_PR = {"number": 7, "title": "Login rework", "body": "Closes #50"}


class _Claude:
    def __init__(self):
        self.calls: list[dict] = []

    def review(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            tool_use_input={"note_html": "<p>n</p>"}, model="claude-sonnet-4-6",
            input_tokens=10, output_tokens=5, cache_read_tokens=0, cache_creation_tokens=0,
        )


def _prompt(ticket_name: str) -> str:
    claude = _Claude()
    note, _cost = build_note(claude, str(PROMPTS_DIR), ticket_name, _PR, "diff --git a b\n+x\n", [])
    assert note == "<p>n</p>"
    return claude.calls[0]["user_prompt"]


def test_the_ticket_name_sets_the_language():
    prompt = _prompt("Anmeldung überarbeiten")
    assert "Odoo ticket name (write the note in ITS language): Anmeldung überarbeiten\n" in prompt
    assert "ticket name unknown" not in prompt


def test_without_a_ticket_name_the_pr_sets_the_language():
    prompt = _prompt("")
    assert (
        "Odoo ticket name unknown: write the note in the language of the PR title and description.\n"
        in prompt
    )
    assert "ITS language" not in prompt
    assert "Merged PR #7: Login rework" in prompt
