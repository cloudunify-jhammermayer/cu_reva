# Committable suggestion patches — Design

- **Date:** 2026-07-11
- **Status:** approved (design), not yet planned
- **Context:** feature brainstorm 2026-07-11 (market research: 1-click committable
  suggestions are table stakes — CodeRabbit, Copilot, Graphite, Bugbot all have them).
  Joseph approved with the condition: per-repo toggle like the other settings.

## Problem

`Finding.suggestion` is rendered as plain text inside the inline comment. GitHub's
native ` ```suggestion ` fenced block renders the fix as a diff with a **"Commit
suggestion"** button — one click applies it to the PR branch. REVA's suggestions
require copy-paste today.

## Design

1. **Formatter** (`reva/review_formatter.py`, inline-comment path): when a finding
   has a non-empty `suggestion` AND a valid single-file line anchor
   (`line_start`..`line_end` on the new side — the same anchor the inline comment
   already uses), render the suggestion as a ` ```suggestion ` block. GitHub
   semantics: the block **replaces exactly the commented line range**, so this is
   only correct when the suggestion is a full replacement for those lines.
2. **Prompt contract** (all four full-diff skills + delta, `prompts/skills/`; new
   prompt version): tighten the `suggestion` field guidance — it must be the exact
   replacement code for the cited line range (matching indentation, no prose, no
   ellipses), or omitted when the fix isn't expressible as a line replacement
   (multi-file, conceptual, or larger-scope advice). Prose advice belongs in
   `description`.
3. **Plausibility guard** (formatter, cheap and deterministic — no model call):
   downgrade to the current plain-text rendering when the suggestion is clearly
   not committable: empty/whitespace, contains markdown fences, longer than
   ~30 lines, or the finding lacks `line_start`/`line_end`. Never drop the
   suggestion — degrade to text.
4. **Toggle**: `RepoConfig.commit_suggestions: bool = True` (`.claude-review.yml`).
   Off → current plain-text rendering everywhere. Documented in the README config
   table.
5. **Injection posture unchanged**: suggestion text is model output already posted
   to GitHub today; the existing internal-path redaction applies to it as to all
   posted text. A committable block adds no new trust: the developer reviews the
   rendered diff and clicks, same as any human-suggested change.

## Explicitly out of scope

- Multi-line-range or multi-file suggestion batches (GitHub can't render them).
- Auto-committing suggestions (that's the separate `/fix` command spec).
- Retro-fitting old findings; applies to newly posted reviews only.

## Testing

- Formatter: committable rendering with anchor+suggestion; each downgrade
  trigger (no anchor, fences, oversize, toggle off) → plain text; suggestion
  block content byte-exact (GitHub is whitespace-sensitive).
- Prompt files: skill output contracts mention the exact-replacement rule
  (extend the existing prompt-content tests).
- One end-to-end reviewer test asserting the posted inline comment body carries
  the fenced block.

## Expected behavior

Findings with line-anchored fixes gain a "Commit suggestion" button; conceptual
findings look exactly as today. No cost change (same model output, stricter
schema guidance). Staging gate: one live review on a linked PR, click the button
once.
