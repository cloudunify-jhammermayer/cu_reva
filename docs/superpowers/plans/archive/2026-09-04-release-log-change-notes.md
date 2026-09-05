# Release-log entries replace drafted change notes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a merged PR's ticket has an entry in the repo's release log (`docs/releases/<name>.md`), REVA ships that entry (Gebaut + To-do) in the ticket-ready "Changes merged" summary instead of a Claude-drafted note; tickets without an entry keep today's behaviour.

**Architecture:** A Markdown parser/renderer for the documented release-log format in `reva/release_log.py`; a worker helper that lists `docs/releases/*.md` on the default branch and finds the ticket's entry; the merge job records a zero-cost `change_notes` row with `source = "release-log"` and skips Claude; the delivery step re-reads the entry and sends it once as the new optional `release_log` block on `tickets.change-summary`. One new column (`change_notes.source`, migration 049). Odoo posts the block once above the PR lines.

**Tech Stack:** Python 3.14 (REVA: `reva/`, `worker/`), plain SQL migration, pytest with SQLite; Odoo 19 module `cu_reva_ticket_analysis` (Python + tests), OCA fastapi.

**Spec:** `docs/superpowers/specs/archive/2026-09-04-release-log-change-notes-design.md`. Release-log format reference: `../wenatex_odoo/docs/releases/lollipop.md` (7 entries; the fixture in Task 1 is cut from it).

## Global Constraints

- **No commits, no push.** Joseph commits. Every task ends with `git add` of its files only. Task 4 syncs contracts into `../Cloudunify` and bumps the pin there, unstaged; Task 5 stages the Odoo work together with them.
- REVA test commands: `cd worker && .venv/bin/python -m pytest tests/<file> -q`, `cd api && .venv/bin/python -m pytest tests/<file> -q`; full `make test` from the repo root; `ruff check reva worker/worker api/app scheduler/scheduler`. A shared `reva/` change needs `make test` (worker, api, scheduler).
- Release-log format (binding, from the wenatex spec and the real file): YAML-ish frontmatter between `---` lines with `release`, `status` (`open|frozen|shipped`), `date`; a `# ` title; `## <ticket number> — <title>` entries (em dash, en dash or hyphen accepted); metadata lines `- Status: <word>` and `- Module: <name> <version>` (several joined by ` · `) before the first `### `; `### Gebaut` (paragraphs or a `- ` list) and `### To-do` (`- ` items, continuation lines indented); `## Entscheidungen` / `## Nicht in diesem Release` and any other non-numeric `## ` heading end the entries. Inline: `**bold**`, `_italic_` (menu paths), `` `code` ``.
- Rendered HTML uses only `p`, `ul`, `li`, `strong`, `em`, `code`; text is HTML-escaped; no links, no classes.
- Wire: `tickets.change-summary` gains optional `release_log` (`release`, `ticket`, `title`, `status`, `modules`, `html`), omitted when None; `notes[].note_html` may be `""`.
- Ops events exactly: `release_log/info/release_log_entry_ambiguous`, `release_log/warning/release_log_parse_failed`, `release_log/warning/release_log_entry_missing`. GitHub `TransientError` propagates (RQ retries); the budget gate is skipped for release-log rows.
- Existing behaviour that must not change: the ready-convergence rule, dedup of `change_notes` on (repo, pr, ticket), `delivered_at` semantics, the Claude path for uncovered tickets, the `/tickets/change-note` legacy path.
- Match neighbouring style; comments in English; nothing beyond the task.

---

### Task 1: Release-log parser and renderer

**Files:**
- Modify: `reva/release_log.py` (append after `theme_css`)
- Test: `worker/tests/test_release_log_parser.py` (new)

**Interfaces:**
- Produces:
  - `ReleaseEntry(ticket: int, title: str, status: str, modules: tuple[str, ...], built: str, todo: str)` (frozen dataclass; `built`/`todo` are raw Markdown bodies, `""` when the section is absent)
  - `ReleaseLogDoc(release: str, status: str, date: str, entries: dict[int, ReleaseEntry])`
  - `ReleaseLogParseError(ValueError)`
  - `parse_release_log(text: str) -> ReleaseLogDoc` (raises `ReleaseLogParseError` when the frontmatter or its `release` key is missing; otherwise lenient)
  - `render_entry_html(entry: ReleaseEntry) -> str`

- [ ] **Step 1: Write the failing tests**

Create `worker/tests/test_release_log_parser.py`:

```python
"""Parser and renderer for the customer repos' release logs (spec 2026-09-04)."""

from __future__ import annotations

import pytest

from reva.release_log import (
    ReleaseLogParseError,
    parse_release_log,
    render_entry_html,
)

LOG = """---
release: lollipop
status: open
date: 2026-09-30
---

# Release Lollipop

Was mit diesem Release kommt, je Ticket.

## 7595 — Hintergrundjob-Warteschlange

- Status: umgesetzt
- Module: cu_queue 19.0.1.2.1

### Gebaut

Importe und andere lange Arbeiten laufen als Hintergrundjobs über den Odoo-Zeitplaner statt im Browser: automatische
Wiederholung bei vorübergehenden Fehlern. Eigene App „Job Queue“ mit Wiederholen & Abbrechen.

Erledigte Jobs werden nach 30 Tagen aufgeräumt.

### To-do

- Geplante Aktion „Job Queue: run background jobs“ einmal prüfen, sie ist standardmäßig aktiv _(Einstellungen →
  Technisch → Automatisierung → Geplante Aktionen)_
- Zeitwerte nur bei Bedarf anpassen: **Requeue** nach 15 min _(Job Queue → Konfiguration)_

## 6965 - Kostenstellen je Mandant

- Status: weitgehend
- Module: cu_import_tools 19.0.4.4.0 · cu_queue 19.0.1.2.1

### Gebaut

- „In Mandanten duplizieren“ legt je Mandant eine Kopie an
- Import liest die Excel-Vorlage in zwei Modi; Zellen wie `A<B` bleiben Text

## Entscheidungen

- **Kontenzusammenführung:** Variante 1 bis 4.

## Nicht in diesem Release

- Skonto.
"""


def test_frontmatter_and_entries_are_parsed():
    doc = parse_release_log(LOG)
    assert (doc.release, doc.status, doc.date) == ("lollipop", "open", "2026-09-30")
    assert sorted(doc.entries) == [6965, 7595]
    entry = doc.entries[7595]
    assert entry.title == "Hintergrundjob-Warteschlange"
    assert entry.status == "umgesetzt"
    assert entry.modules == ("cu_queue 19.0.1.2.1",)
    assert entry.built.startswith("Importe und andere lange Arbeiten")
    assert "Erledigte Jobs" in entry.built
    assert entry.todo.startswith("- Geplante Aktion")


def test_hyphen_headings_and_several_modules():
    entry = parse_release_log(LOG).entries[6965]
    assert entry.title == "Kostenstellen je Mandant"
    assert entry.modules == ("cu_import_tools 19.0.4.4.0", "cu_queue 19.0.1.2.1")
    assert entry.todo == ""


def test_trailing_sections_are_not_entries():
    doc = parse_release_log(LOG)
    assert all(isinstance(k, int) for k in doc.entries)
    assert "Kontenzusammenführung" not in doc.entries[6965].built


def test_frontmatter_comments_are_ignored():
    text = "---\nrelease: marshmallow\nstatus: open          # open | frozen | shipped\ndate: 2026-10-30\n---\n# R\n"
    doc = parse_release_log(text)
    assert (doc.release, doc.status, doc.date) == ("marshmallow", "open", "2026-10-30")
    assert doc.entries == {}


def test_missing_frontmatter_or_release_raises():
    with pytest.raises(ReleaseLogParseError):
        parse_release_log("# Release X\n\n## 1 — a\n")
    with pytest.raises(ReleaseLogParseError):
        parse_release_log("---\nstatus: open\n---\n# R\n")


def test_render_paragraphs_lists_and_inline_marks():
    entry = parse_release_log(LOG).entries[7595]
    html = render_entry_html(entry)
    assert html.startswith("<p><strong>Gebaut</strong></p><p>Importe und andere lange Arbeiten")
    assert "Wiederholen &amp; Abbrechen.</p><p>Erledigte Jobs werden nach 30 Tagen aufgeräumt.</p>" in html
    assert "<p><strong>To-do</strong></p><ul><li>Geplante Aktion" in html
    assert "<em>(Einstellungen → Technisch → Automatisierung → Geplante Aktionen)</em></li>" in html
    assert "<strong>Requeue</strong> nach 15 min <em>(Job Queue → Konfiguration)</em></li></ul>" in html
    assert "\n" not in html


def test_render_list_built_and_escaping_and_no_todo_section():
    entry = parse_release_log(LOG).entries[6965]
    html = render_entry_html(entry)
    assert html.startswith("<p><strong>Gebaut</strong></p><ul><li>„In Mandanten duplizieren“")
    assert "Zellen wie <code>A&lt;B</code> bleiben Text</li></ul>" in html
    assert "To-do" not in html


def test_render_empty_entry_is_empty():
    from reva.release_log import ReleaseEntry

    assert render_entry_html(ReleaseEntry(1, "t", "", (), "", "")) == ""
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_release_log_parser.py -q`
Expected: ImportError on `ReleaseLogParseError`.

- [ ] **Step 3: Implement**

Append to `reva/release_log.py` (add `import html as html_lib` and `from dataclasses import dataclass` to the imports):

```python


# --- Release-log entries (spec 2026-09-04-release-log-change-notes) -----------
#
# The customer repos' docs/releases/<name>.md is the developer-written source
# of what shipped per ticket. Parsed here so the merged-PR summary Odoo posts
# carries that text instead of a Claude draft.

_FRONTMATTER_FENCE = "---"
_ENTRY_HEADING_RE = re.compile(r"^##\s+(\d+)\s*[—–-]\s*(.+?)\s*$")
_OTHER_H2_RE = re.compile(r"^##\s+")
_H3_RE = re.compile(r"^###\s+(.+?)\s*$")
_META_RE = re.compile(r"^-\s+(Status|Module):\s*(.*?)\s*$")
_LIST_ITEM_RE = re.compile(r"^-\s+(.*)$")
_STRONG_RE = re.compile(r"\*\*(.+?)\*\*")
_EM_RE = re.compile(r"(?<![\w*])_(.+?)_(?![\w*])")
_CODE_RE = re.compile(r"`([^`]+)`")


class ReleaseLogParseError(ValueError):
    """The text is not a release log: no frontmatter, or no `release` key."""


@dataclass(frozen=True)
class ReleaseEntry:
    ticket: int
    title: str
    status: str
    modules: tuple[str, ...]
    built: str  # raw Markdown of "### Gebaut", "" when absent
    todo: str  # raw Markdown of "### To-do", "" when absent


@dataclass(frozen=True)
class ReleaseLogDoc:
    release: str
    status: str
    date: str
    entries: dict[int, ReleaseEntry]


def _parse_frontmatter(lines: list[str]) -> tuple[dict[str, str], int]:
    """(key -> value, index of the first body line). Values may carry a
    trailing `# comment` (the wenatex spec's example does)."""
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        raise ReleaseLogParseError("no frontmatter")
    meta: dict[str, str] = {}
    for i in range(1, len(lines)):
        line = lines[i].strip()
        if line == _FRONTMATTER_FENCE:
            return meta, i + 1
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.split(" #", 1)[0].strip()
    raise ReleaseLogParseError("unterminated frontmatter")


def parse_release_log(text: str) -> ReleaseLogDoc:
    """Parse a release log into its entries, keyed by ticket number.

    Lenient beyond the frontmatter: unknown metadata, extra `###` sections
    and prose outside entries are ignored, and the trailing `##` sections
    (Entscheidungen, Nicht in diesem Release) simply end the entry list."""
    lines = text.splitlines()
    meta, start = _parse_frontmatter(lines)
    if not meta.get("release"):
        raise ReleaseLogParseError("frontmatter has no release")

    entries: dict[int, ReleaseEntry] = {}
    current: dict | None = None
    section: str | None = None

    def _close() -> None:
        if current is not None:
            entries[current["ticket"]] = ReleaseEntry(
                ticket=current["ticket"],
                title=current["title"],
                status=current["status"],
                modules=tuple(current["modules"]),
                built="\n".join(current["built"]).strip(),
                todo="\n".join(current["todo"]).strip(),
            )

    for raw in lines[start:]:
        line = raw.rstrip()
        heading = _ENTRY_HEADING_RE.match(line)
        if heading:
            _close()
            current = {
                "ticket": int(heading.group(1)),
                "title": heading.group(2),
                "status": "",
                "modules": [],
                "built": [],
                "todo": [],
            }
            section = None
            continue
        if _OTHER_H2_RE.match(line):
            _close()
            current = None
            section = None
            continue
        if current is None:
            continue
        h3 = _H3_RE.match(line)
        if h3:
            name = h3.group(1).casefold()
            section = "built" if name == "gebaut" else "todo" if name == "to-do" else "other"
            continue
        if section is None:
            meta_line = _META_RE.match(line)
            if meta_line:
                if meta_line.group(1) == "Status":
                    current["status"] = meta_line.group(2)
                else:
                    current["modules"] = [
                        m.strip() for m in meta_line.group(2).split("·") if m.strip()
                    ]
            continue
        if section in ("built", "todo"):
            current[section].append(line)
    _close()
    return ReleaseLogDoc(
        release=meta.get("release", ""),
        status=meta.get("status", ""),
        date=meta.get("date", ""),
        entries=entries,
    )


def _inline_html(text: str) -> str:
    """Escape, then the three inline marks the format uses."""
    out = html_lib.escape(text, quote=False)
    out = _CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _STRONG_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = _EM_RE.sub(lambda m: f"<em>{m.group(1)}</em>", out)
    return out


def _block_html(markdown: str) -> str:
    """Paragraphs -> <p>, `- ` lists -> <ul><li> (indented continuation lines
    belong to the item), everything joined without newlines."""
    parts: list[str] = []
    for para in re.split(r"\n\s*\n", markdown.strip()):
        lines = [ln for ln in para.splitlines() if ln.strip()]
        if not lines:
            continue
        if all(_LIST_ITEM_RE.match(ln) or ln.startswith((" ", "\t")) for ln in lines) and _LIST_ITEM_RE.match(
            lines[0]
        ):
            items: list[str] = []
            for ln in lines:
                item = _LIST_ITEM_RE.match(ln)
                if item:
                    items.append(item.group(1).strip())
                else:
                    items[-1] = f"{items[-1]} {ln.strip()}"
            parts.append("<ul>" + "".join(f"<li>{_inline_html(i)}</li>" for i in items) + "</ul>")
        else:
            parts.append("<p>" + _inline_html(" ".join(ln.strip() for ln in lines)) + "</p>")
    return "".join(parts)


def render_entry_html(entry: ReleaseEntry) -> str:
    """The chatter body for one entry: Gebaut then To-do, each with a bold
    caption; a section without content is omitted."""
    out = ""
    if entry.built:
        out += "<p><strong>Gebaut</strong></p>" + _block_html(entry.built)
    if entry.todo:
        out += "<p><strong>To-do</strong></p>" + _block_html(entry.todo)
    return out
```

- [ ] **Step 4: Run the tests**

Run: `cd worker && .venv/bin/python -m pytest tests/test_release_log_parser.py tests/test_release_log.py -q`
Expected: all pass (if a rendering assertion differs only in whitespace, fix the renderer, not the test).

- [ ] **Step 5: Full check and stage**

Run: `make test && ruff check reva worker/worker api/app scheduler/scheduler`
```bash
git add reva/release_log.py worker/tests/test_release_log_parser.py
```

---

### Task 2: `change_notes.source` column and repository lookup

**Files:**
- Create: `db/migrations/049_change_notes_source.sql`
- Modify: `reva/db/models.py` (`ChangeNote`), `reva/db/writers.py` (`_change_note_dict`, `record_change_note_completed`, `get_undelivered_change_notes`, new `get_repository_by_full_name`)
- Test: `worker/tests/test_release_note_writers.py` (append), `worker/tests/test_change_note_delivery.py` (the `_seed_note` helper gains `source`)

**Interfaces:**
- Produces: `ChangeNote.source: str` (`"claude"` default, `"release-log"`); `record_change_note_completed(db, note_id, note_html, cost, source="claude")`; `get_undelivered_change_notes(...)` items carry `source`; `_change_note_dict` carries `source`; `writers.get_repository_by_full_name(db, full_name) -> dict | None` with keys `id, owner, name, full_name, default_branch, installation_id, enabled` (case-insensitive match).

- [ ] **Step 1: Write the failing tests**

Append to `worker/tests/test_release_note_writers.py`:

```python


def test_change_note_source_defaults_to_claude_and_records_release_log(db):
    note_id, row = writers.get_or_create_change_note(
        db, "acme/widgets", 7, 97, 1, "helpdesk.ticket", pr_title="t", pr_url="u"
    )
    assert row["source"] == "claude"
    writers.record_change_note_completed(db, note_id, "", 0.0, source="release-log")
    notes = writers.get_undelivered_change_notes(db, 1, 97, "helpdesk.ticket")
    # get_undelivered_change_notes does not check readiness; "" is not None, so the row is listed
    assert [(n["pr_number"], n["source"], n["note_html"]) for n in notes] == [(7, "release-log", "")]


def test_get_repository_by_full_name_is_case_insensitive(db):
    with db.session() as s:
        s.add(Repository(id=5, github_repository_id=1005, owner="Acme", name="Widgets",
                         full_name="Acme/Widgets", installation_id=7, enabled=True,
                         default_branch="dev"))
    row = writers.get_repository_by_full_name(db, "acme/widgets")
    assert row == {"id": 5, "owner": "Acme", "name": "Widgets", "full_name": "Acme/Widgets",
                   "default_branch": "dev", "installation_id": 7, "enabled": True}
    assert writers.get_repository_by_full_name(db, "acme/other") is None
```
(`Repository` is already imported in that file.)

- [ ] **Step 2: Run them to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_release_note_writers.py -q`
Expected: KeyError `source` / AttributeError `get_repository_by_full_name`.

- [ ] **Step 3: Migration and model**

Create `db/migrations/049_change_notes_source.sql`:
```sql
-- Where a change note's text came from (spec 2026-09-04-release-log-change-notes):
-- 'claude' (drafted from the PR diff, today's rows) or 'release-log' (the ticket's
-- entry in the repo's docs/releases/<name>.md; note_html stays empty because the
-- entry is re-read at delivery time and sent once per ticket).
-- Mirrors reva/db/models.py::ChangeNote.source.
ALTER TABLE change_notes ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'claude';
```
In `reva/db/models.py::ChangeNote`, after `note_html`:
```python
    # 'claude' (drafted from the diff) or 'release-log' (the ticket's entry in
    # docs/releases/<name>.md, re-read at delivery; note_html stays "").
    source: Mapped[str] = mapped_column(Text, nullable=False, default="claude")
```

- [ ] **Step 4: Writers**

`_change_note_dict`: add `"source": row.source,`. `record_change_note_completed(db, note_id, note_html, cost, source="claude")`: set `row.source = source`. `get_undelivered_change_notes`: add `"source": row.source,` to the returned dicts. Add after `mark_change_notes_delivered`:

```python
def get_repository_by_full_name(db: Database, full_name: str) -> dict | None:
    """The registered repo for an `owner/name` (case-insensitive), or None."""
    with db.session() as s:
        row = s.execute(
            select(Repository).where(func.lower(Repository.full_name) == full_name.lower())
        ).scalars().first()
        if row is None:
            return None
        return {
            "id": row.id,
            "owner": row.owner,
            "name": row.name,
            "full_name": row.full_name,
            "default_branch": row.default_branch or "main",
            "installation_id": row.installation_id,
            "enabled": row.enabled,
        }
```
(`func` and `select` are already imported in writers.)

- [ ] **Step 5: Run the tests, then the whole suite**

Run: `cd worker && .venv/bin/python -m pytest tests/test_release_note_writers.py tests/test_change_note_delivery.py -q`, then `make test && ruff check reva worker/worker api/app scheduler/scheduler`.

```bash
git add db/migrations/049_change_notes_source.sql reva/db/models.py reva/db/writers.py worker/tests/test_release_note_writers.py
```

---

### Task 3: Merge job uses the release-log entry; delivery sends it once

**Files:**
- Create: `worker/worker/release_log_lookup.py`
- Modify: `worker/worker/change_note_runner.py`, `worker/worker/change_note_delivery.py`
- Modify: `reva/odoo_client.py` (`change_summary` gains `release_log=None`) — payload model comes in Task 4, so in this task the client passes `release_log` only when not None and the model still lacks it: do the client change in Task 4 instead; here the delivery calls `odoo.change_summary(..., release_log=block)` and the test double accepts it. **Execution order: 1, 2, 4, 3, 5, 6** (Task 3 calls the client signature Task 4 adds).
- Test: `worker/tests/test_change_note_delivery.py` (append), `worker/tests/test_release_log_lookup.py` (new)

**Interfaces:**
- Consumes: Task 1 parser/renderer; Task 2 writers; Task 4 `OdooCallbackClient.change_summary(ticket_id, model_name, notes, release_log=None)`.
- Produces:
  - `worker.release_log_lookup.find_release_entry(ctx, token: str, owner: str, name: str, ref: str, ticket_id: int, log) -> tuple[ReleaseLogDoc, ReleaseEntry] | None`
  - `worker.release_log_lookup.release_log_block(ctx, repo_full_name: str, ticket_id: int, log) -> dict | None` (resolves repo + token, returns the wire block or None)

- [ ] **Step 1: Write the failing tests**

Create `worker/tests/test_release_log_lookup.py`:

```python
"""Finding a ticket's release-log entry in a repo (spec 2026-09-04-release-log-change-notes)."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from reva.db import Base, Database, create_engine_from_url
from reva.db.models import OpsEvent, Repository
from reva.errors import TransientError
from worker.release_log_lookup import find_release_entry, release_log_block

OPEN = "---\nrelease: lollipop\nstatus: open\ndate: 2026-09-30\n---\n# R\n\n## 7595 — Warteschlange\n\n- Status: umgesetzt\n- Module: cu_queue 19.0.1.2.1\n\n### Gebaut\n\nLäuft im Hintergrund.\n\n### To-do\n\n- Cron prüfen _(Einstellungen)_\n"
SHIPPED = "---\nrelease: kiwi\nstatus: shipped\ndate: 2026-06-30\n---\n# R\n\n## 7595 — Alte Fassung\n\n- Status: umgesetzt\n\n### Gebaut\n\nAlt.\n"
BROKEN = "# no frontmatter\n\n## 7595 — x\n"


@dataclass
class FakeGitHub:
    files: dict[str, str] = field(default_factory=dict)  # path -> text on ref "dev"
    raise_exc: Exception | None = None

    def get_installation_token(self, installation_id):
        return f"tok-{installation_id}"

    def get_tree(self, token, owner, repo, ref, recursive=True):
        if self.raise_exc:
            raise self.raise_exc
        return {"tree": [{"path": p, "type": "blob"} for p in self.files] + [{"path": "docs/releases", "type": "tree"}], "truncated": False}

    def get_file_content(self, token, owner, repo, path, ref):
        assert ref == "dev"
        return self.files.get(path)


@pytest.fixture()
def env():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Database(engine)
    with db.session() as s:
        s.add(Repository(id=3, github_repository_id=1003, owner="acme", name="widgets",
                         full_name="acme/widgets", installation_id=7, enabled=True, default_branch="dev"))
    gh = FakeGitHub()
    return {"db": db, "github": gh, "ctx": SimpleNamespace(db=db, github=gh)}


def _events(db):
    with db.session() as s:
        return [(e.component, e.severity, e.event) for e in s.execute(select(OpsEvent)).scalars()]


def test_finds_the_entry_in_the_open_log(env):
    env["github"].files = {"docs/releases/lollipop.md": OPEN, "docs/releases/notes.txt": "x"}
    doc, entry = find_release_entry(env["ctx"], "tok", "acme", "widgets", "dev", 7595, None)
    assert (doc.release, entry.title) == ("lollipop", "Warteschlange")
    assert _events(env["db"]) == []


def test_missing_entry_is_none(env):
    env["github"].files = {"docs/releases/lollipop.md": OPEN}
    assert find_release_entry(env["ctx"], "tok", "acme", "widgets", "dev", 1, None) is None


def test_open_log_wins_over_shipped_and_ambiguity_is_recorded(env):
    env["github"].files = {"docs/releases/kiwi.md": SHIPPED, "docs/releases/lollipop.md": OPEN}
    doc, entry = find_release_entry(env["ctx"], "tok", "acme", "widgets", "dev", 7595, None)
    assert doc.release == "lollipop"
    assert ("release_log", "info", "release_log_entry_ambiguous") in _events(env["db"])


def test_unparseable_log_is_skipped_with_event(env):
    env["github"].files = {"docs/releases/broken.md": BROKEN, "docs/releases/lollipop.md": OPEN}
    doc, entry = find_release_entry(env["ctx"], "tok", "acme", "widgets", "dev", 7595, None)
    assert doc.release == "lollipop"
    assert ("release_log", "warning", "release_log_parse_failed") in _events(env["db"])


def test_github_outage_propagates(env):
    env["github"].raise_exc = TransientError("GitHub 502")
    with pytest.raises(TransientError):
        find_release_entry(env["ctx"], "tok", "acme", "widgets", "dev", 7595, None)


def test_release_log_block_resolves_repo_and_renders(env):
    env["github"].files = {"docs/releases/lollipop.md": OPEN}
    block = release_log_block(env["ctx"], "ACME/Widgets", 7595, None)
    assert block == {
        "release": "lollipop",
        "ticket": 7595,
        "title": "Warteschlange",
        "status": "umgesetzt",
        "modules": ["cu_queue 19.0.1.2.1"],
        "html": "<p><strong>Gebaut</strong></p><p>Läuft im Hintergrund.</p>"
        "<p><strong>To-do</strong></p><ul><li>Cron prüfen <em>(Einstellungen)</em></li></ul>",
    }


def test_release_log_block_is_none_for_unknown_repo_or_missing_entry(env):
    env["github"].files = {"docs/releases/lollipop.md": OPEN}
    assert release_log_block(env["ctx"], "acme/unknown", 7595, None) is None
    assert release_log_block(env["ctx"], "acme/widgets", 4242, None) is None
```

Append to `worker/tests/test_change_note_delivery.py` (the `_seed_note` helper gains a `source="claude"` parameter passed to `ChangeNote(...)`; `FakeOdoo.change_summary` gains `release_log=None` and records it):

```python


# --- release-log entries instead of Claude drafts (spec 2026-09-04) -----------

_OPEN_LOG = (
    "---\nrelease: lollipop\nstatus: open\ndate: 2026-09-30\n---\n# R\n\n"
    "## 97 — Login\n\n- Status: umgesetzt\n- Module: cu_auth 19.0.1.0.0\n\n"
    "### Gebaut\n\nNeue Anmeldung.\n\n### To-do\n\n- Rollen prüfen\n"
)


def _seed_repo(db):
    from reva.db.models import Repository

    with db.session() as s:
        s.add(Repository(id=3, github_repository_id=1003, owner="acme", name="widgets",
                         full_name="acme/widgets", installation_id=99, enabled=True,
                         default_branch="main"))


def _with_release_log(cn_ctx, text=_OPEN_LOG):
    gh = cn_ctx["github"]
    gh.get_tree.return_value = {"tree": [{"path": "docs/releases/lollipop.md", "type": "blob"}], "truncated": False}
    gh.get_file_content.return_value = text
    _seed_repo(cn_ctx["db"])


def test_covered_ticket_skips_claude_and_records_release_log_source(cn_ctx, monkeypatch):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _with_release_log(s)
    _seed_run(s["db"], issues=[{"number": 50, "state": "open"}])
    monkeypatch.setattr("worker.change_note_runner.build_note",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Claude must not be called")))
    out = run_change_note(_cn_params())
    assert out == {"status": "completed", "delivered": 0}
    row = _note_rows(s["db"])[0]
    assert (row.status, row.source, row.note_html, float(row.estimated_cost_usd)) == ("completed", "release-log", "", 0.0)
    s["github"].get_pull_request_diff.assert_not_called()


def test_uncovered_ticket_still_drafts_with_claude(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _with_release_log(s, text=_OPEN_LOG.replace("## 97 — Login", "## 4242 — Other"))
    _seed_run(s["db"], issues=[{"number": 50, "state": "open"}])
    run_change_note(_cn_params())
    row = _note_rows(s["db"])[0]
    assert (row.source, row.note_html) == ("claude", "<p>merged</p>")


def test_delivery_sends_the_entry_once_with_empty_pr_notes(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _with_release_log(s)
    _seed_run(s["db"], issues=[{"number": 50, "state": "closed"}])  # ready
    out = run_change_note(_cn_params())
    assert out["delivered"] == 1
    call = s["odoo"].calls[0]
    assert call["notes"] == [{"pr": {"number": 7, "title": "Login rework",
                                     "url": "https://github.com/acme/widgets/pull/7", "repo": "acme/widgets"},
                              "note_html": ""}]
    assert call["release_log"]["ticket"] == 97
    assert call["release_log"]["title"] == "Login"
    assert call["release_log"]["html"].startswith("<p><strong>Gebaut</strong></p><p>Neue Anmeldung.</p>")
    assert call["release_log"]["modules"] == ["cu_auth 19.0.1.0.0"]


def test_delivery_without_release_log_rows_sends_no_block(db):
    _ready_run(db)
    _seed_note(db, pr_number=1)
    odoo = FakeOdoo()
    assert _deliver(db, odoo) is True
    assert odoo.calls[0]["release_log"] is None


def test_entry_missing_at_delivery_sends_without_block_and_records_event(db, monkeypatch):
    _ready_run(db)
    _seed_repo(db)
    _seed_note(db, pr_number=1, note_html="", source="release-log")
    gh = MagicMock()
    gh.get_installation_token.return_value = "tok"
    gh.get_tree.return_value = {"tree": [], "truncated": False}
    odoo = FakeOdoo()
    assert maybe_deliver_change_notes(SimpleNamespace(db=db, github=gh), odoo, _INSTANCE, _TICKET, _MODEL) is True
    assert odoo.calls[0]["release_log"] is None
    assert "release_log_entry_missing" in _ops_events(db)


def test_release_log_rows_with_empty_html_are_still_delivered(db):
    _ready_run(db)
    _seed_note(db, pr_number=1, note_html="", source="release-log")
    assert writers_undelivered(db) == [1]


def writers_undelivered(db):
    from reva.db import writers

    return [n["pr_number"] for n in writers.get_undelivered_change_notes(db, _INSTANCE, _TICKET, _MODEL)]
```
In `_deliver`, pass `SimpleNamespace(db=db, github=MagicMock())` so the release-log branch (never reached for Claude rows) has a `github` attribute.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_release_log_lookup.py tests/test_change_note_delivery.py -q`
Expected: ImportError on `worker.release_log_lookup`; the delivery tests fail on the missing `release_log` key.

- [ ] **Step 3: The lookup module**

Create `worker/worker/release_log_lookup.py`:

```python
"""Find a ticket's entry in a repo's release logs (spec 2026-09-04-release-log-change-notes).

The customer repo keeps docs/releases/<name>.md, one entry per ticket. The merge
job asks whether the ticket has one (then Claude is not needed) and the ready-
time delivery re-reads it so the customer gets the final text. Read from the
repo's default branch through the GitHub API, the same way the docs site does.
"""

from __future__ import annotations

import posixpath

import structlog

from reva.db import writers
from reva.errors import TransientError
from reva.release_log import (
    ReleaseEntry,
    ReleaseLogDoc,
    ReleaseLogParseError,
    parse_release_log,
    render_entry_html,
)

logger = structlog.get_logger()

_RELEASES_DIR = "docs/releases"


def _release_log_paths(tree: dict) -> list[str]:
    paths = []
    for entry in tree.get("tree", []):
        path = entry.get("path", "")
        if (
            entry.get("type") == "blob"
            and posixpath.dirname(path) == _RELEASES_DIR
            and path.endswith(".md")
        ):
            paths.append(path)
    return sorted(paths)


def find_release_entry(
    ctx, token: str, owner: str, name: str, ref: str, ticket_id: int, log
) -> tuple[ReleaseLogDoc, ReleaseEntry] | None:
    """(doc, entry) for the ticket, or None. Open logs win over frozen/shipped
    ones, then the alphabetically first file; several hits are an ops event so
    the duplicate gets cleaned up. An unparseable log is skipped with an ops
    event. GitHub errors propagate (a TransientError makes RQ retry the job)."""
    log = log or logger
    tree = ctx.github.get_tree(token, owner, name, ref)
    hits: list[tuple[str, ReleaseLogDoc, ReleaseEntry]] = []
    for path in _release_log_paths(tree):
        text = ctx.github.get_file_content(token, owner, name, path, ref)
        if text is None:
            continue
        try:
            doc = parse_release_log(text)
        except ReleaseLogParseError as exc:
            log.warning("release_log_parse_failed", repo=f"{owner}/{name}", path=path, error=str(exc))
            writers.record_ops_event(
                ctx.db, "release_log", "warning", "release_log_parse_failed",
                {"repo": f"{owner}/{name}", "path": path, "error": str(exc)[:300]},
            )
            continue
        entry = doc.entries.get(ticket_id)
        if entry is not None:
            hits.append((path, doc, entry))
    if not hits:
        return None
    if len(hits) > 1:
        writers.record_ops_event(
            ctx.db, "release_log", "info", "release_log_entry_ambiguous",
            {"repo": f"{owner}/{name}", "ticket_id": ticket_id, "paths": [h[0] for h in hits]},
        )
    hits.sort(key=lambda h: (h[1].status != "open", h[0]))
    _, doc, entry = hits[0]
    return doc, entry


def release_log_block(ctx, repo_full_name: str, ticket_id: int, log) -> dict | None:
    """The `release_log` block of the change-summary callback for a ticket, or
    None when the repo is unknown or carries no entry for it."""
    repo = writers.get_repository_by_full_name(ctx.db, repo_full_name)
    if repo is None:
        return None
    token = ctx.github.get_installation_token(repo["installation_id"])
    found = find_release_entry(
        ctx, token, repo["owner"], repo["name"], repo["default_branch"], ticket_id, log
    )
    if found is None:
        return None
    doc, entry = found
    return {
        "release": doc.release,
        "ticket": ticket_id,
        "title": entry.title,
        "status": entry.status,
        "modules": list(entry.modules),
        "html": render_entry_html(entry),
    }


__all__ = ["find_release_entry", "release_log_block", "TransientError"]
```
(Drop `TransientError` from `__all__` and the import if ruff flags it unused.)

- [ ] **Step 4: Merge job**

In `worker/worker/change_note_runner.py`: import `from worker.release_log_lookup import release_log_block`. Change the regenerate condition and add the release-log branch:

```python
        if not (row["status"] == "completed" and row["note_html"] is not None):
            # The ticket's own release-log entry beats a drafted note: zero cost,
            # written by the developer, re-read at delivery time.
            block = release_log_block(ctx, repo, ref.ticket_id, logger)  # TransientError -> RQ retry
            if block is not None:
                writers.record_change_note_completed(ctx.db, note_id, "", 0.0, source="release-log")
                if maybe_deliver_change_notes(
                    ctx, odoo, ref.odoo_instance_id, ref.ticket_id, ref.model_name, logger
                ):
                    delivered += 1
                continue
            spent = budget_exceeded(ctx)
            ...  (unchanged from here)
```
Keep the rest of the loop untouched. Note the condition change (`is not None` instead of truthiness) so a completed release-log row with `note_html == ""` is not regenerated on a retry.

- [ ] **Step 5: Delivery**

In `worker/worker/change_note_delivery.py`: import `from worker.release_log_lookup import release_log_block`; after building `payload`, before the `try`:

```python
    release_log = None
    if any(note["source"] == "release-log" for note in notes):
        # Re-read at delivery so later PRs' edits to the entry are what ships.
        release_log = release_log_block(ctx, notes[0]["repo_full_name"], ticket_id, log)
        if release_log is None:
            log.warning("release_log_entry_missing", ticket_id=ticket_id)
            writers.record_ops_event(
                ctx.db, "release_log", "warning", "release_log_entry_missing",
                {"ticket_id": ticket_id, "repo": notes[0]["repo_full_name"]},
            )
```
and call `odoo.change_summary(ticket_id=ticket_id, model_name=model_name, notes=payload, release_log=release_log)`. Extend the module docstring with two sentences on the block. A `TransientError` from the lookup propagates like the callback's does (RQ retries; the rows stay undelivered).

- [ ] **Step 6: Run the tests, then everything**

Run: `cd worker && .venv/bin/python -m pytest tests/test_release_log_lookup.py tests/test_change_note_delivery.py -q`, then `make test && ruff check reva worker/worker api/app scheduler/scheduler`.

```bash
git add worker/worker/release_log_lookup.py worker/worker/change_note_runner.py worker/worker/change_note_delivery.py worker/tests/test_release_log_lookup.py worker/tests/test_change_note_delivery.py
```

---

### Task 4: Contract and client (`release_log` on `tickets.change-summary`)

**Files:**
- Modify: `reva/odoo_contracts.py` (`ReleaseLogEntryPayload`, `ChangeSummaryPayload.release_log`, the contract sample + extra sample)
- Modify: `reva/odoo_client.py` (`change_summary(..., release_log=None)`, `exclude_none`)
- Regenerate: `contracts/`; sync to `../Cloudunify`; bump `../Cloudunify/custom_addons/cu_reva_connector/tests/test_contracts.py:11`
- Test: `worker/tests/test_odoo_client.py` (append)

**Interfaces:**
- Produces: `ReleaseLogEntryPayload(release: str, ticket: int, title: str, status: str, modules: list[str], html: str)`; `ChangeSummaryPayload.release_log: ReleaseLogEntryPayload | None = None`; `OdooCallbackClient.change_summary(ticket_id, model_name, notes, release_log=None)` posting `payload.model_dump(exclude_none=True)`.

- [ ] **Step 1: Write the failing test**

Append to `worker/tests/test_odoo_client.py`:

```python
def test_change_summary_carries_the_release_log_block_only_when_given(monkeypatch):
    seen: list[dict] = []

    def post(url, **kwargs):
        seen.append(kwargs["json"])
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    notes = [{"pr": {"number": 7, "title": "t", "url": "https://github.com/acme/widgets/pull/7",
                     "repo": "acme/widgets"}, "note_html": ""}]
    client = _client()
    client.change_summary(ticket_id=97, model_name="helpdesk.ticket", notes=notes)
    client.change_summary(
        ticket_id=97, model_name="helpdesk.ticket", notes=notes,
        release_log={"release": "lollipop", "ticket": 97, "title": "Login", "status": "umgesetzt",
                     "modules": ["cu_auth 19.0.1.0.0"], "html": "<p><strong>Gebaut</strong></p><p>x</p>"},
    )
    assert "release_log" not in seen[0]
    assert seen[0]["notes"][0]["note_html"] == ""
    assert seen[1]["release_log"]["title"] == "Login"
    assert seen[1]["release_log"]["modules"] == ["cu_auth 19.0.1.0.0"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd worker && .venv/bin/python -m pytest tests/test_odoo_client.py -k release_log_block -q`
Expected: TypeError (unexpected keyword `release_log`).

- [ ] **Step 3: Contract models and samples**

In `reva/odoo_contracts.py`, before `class ChangeSummaryPayload`:
```python
class ReleaseLogEntryPayload(BaseModel):
    """The ticket's entry in the repo's release log (docs/releases/<name>.md),
    rendered to simple HTML (p, ul, li, strong, em, code). Sent once per
    ticket on the change summary when the entry exists; the per-PR notes then
    carry an empty note_html (spec 2026-09-04-release-log-change-notes)."""

    release: str
    ticket: int
    title: str
    status: str
    modules: list[str]
    html: str
```
and on `ChangeSummaryPayload`:
```python
    # Present when the repo's release log covers the ticket; omitted otherwise.
    release_log: ReleaseLogEntryPayload | None = None
```
In the `tickets.change-summary` contract: the `sample` gains
```python
            "release_log": {
                "release": "lollipop",
                "ticket": 123,
                "title": "Login rework",
                "status": "umgesetzt",
                "modules": ["cu_auth 19.0.1.0.0"],
                "html": "<p><strong>Gebaut</strong></p><p>Neue Anmeldung mit Rollenprüfung.</p>"
                "<p><strong>To-do</strong></p><ul><li>Rollen prüfen <em>(Einstellungen → Benutzer)</em></li></ul>",
            },
```
and the sample's note gets `"note_html": ""`; add `extra_samples=[{... the previous sample verbatim: ticket_id 123, model_name, one note with "<p>Die Änderung wurde gemerged.</p>", no release_log ...}]` so the legacy shape stays published.

- [ ] **Step 4: Client**

`reva/odoo_client.py::change_summary(self, ticket_id, model_name, notes, release_log=None)`: build `ChangeSummaryPayload(..., release_log=release_log)` and post `payload.model_dump(exclude_none=True)`; docstring: "one note per PR, plus the ticket's release-log entry once when the repo has one".

- [ ] **Step 5: Regenerate, test, sync**

From the repo root: `worker/.venv/bin/python -m reva.odoo_contracts generate`; `cd worker && .venv/bin/python -m pytest tests/test_odoo_client.py tests/test_contracts_drift.py tests/test_odoo_contracts.py tests/test_contracts_generator.py -q`; `cd ../api && .venv/bin/python -m pytest tests/test_contracts_inbound.py -q`. Then `scripts/sync_contracts.sh ../Cloudunify` and put the printed hash into `../Cloudunify/custom_addons/cu_reva_connector/tests/test_contracts.py` line 11 (leave unstaged there). Run `make test && ruff check reva worker/worker api/app scheduler/scheduler`.

```bash
git add reva/odoo_contracts.py reva/odoo_client.py worker/tests/test_odoo_client.py contracts/
```

---

### Task 5: Odoo posts the release-log entry once (module 19.0.55.3.0)

**Files (repo `/home/joseph/Projects/Cloudunify/Cloudunify`):**
- Modify: `custom_addons/cu_reva_ticket_analysis/routers/reva_router.py` (`ReleaseLogBlock`, `ChangeSummaryRequest.release_log`, the route call)
- Modify: `custom_addons/cu_reva_ticket_analysis/models/reva_mixin.py` (`_apply_reva_change_summary(notes, release_log=None)`)
- Modify: `custom_addons/cu_reva_ticket_analysis/tests/test_callback.py` (`TestRevaChangeSummaryCallback`), `tests/test_contracts.py` (`test_change_summary_sample_is_accepted` asserts the block text)
- Modify: `__manifest__.py` (19.0.55.3.0), `CLAUDE.md` (version line + the `/change-summary` sentence), `README.md` (the "Batched changes merged note" paragraph)
- Stage with: `reva_contracts/` and the connector pin (synced in Task 4)

**Interfaces:**
- Consumes: the wire block from Task 4 (`release_log` optional; `note_html` may be `""`).

- [ ] **Step 1: Write the failing tests**

In `tests/test_callback.py::TestRevaChangeSummaryCallback` add:

```python
    def _release_log(self):
        return {
            "release": "lollipop",
            "ticket": self.ticket.id,
            "title": "Login",
            "status": "umgesetzt",
            "modules": ["cu_auth 19.0.1.0.0", "cu_queue 19.0.1.2.1"],
            "html": "<p><strong>Gebaut</strong></p><p>Neue Anmeldung.</p><script>alert(1)</script>"
            "<p><strong>To-do</strong></p><ul><li>Rollen prüfen <em>(Einstellungen)</em></li></ul>",
        }

    def test_summary_posts_the_release_log_entry_once_above_the_prs(self):
        payload = self._payload(release_log=self._release_log())
        for note in payload["notes"]:
            note["note_html"] = ""
        before = len(self.ticket.message_ids)
        resp = self._post(payload)
        self.assertEqual(resp.status_code, 200)
        self.ticket.invalidate_recordset()
        self.assertEqual(len(self.ticket.message_ids), before + 1)
        body = self.ticket.message_ids[:1].body
        self.assertIn("Changes merged", body)
        self.assertIn("Login", body)
        self.assertIn("Release lollipop", body)
        self.assertIn("umgesetzt", body)
        self.assertIn("cu_auth 19.0.1.0.0, cu_queue 19.0.1.2.1", body)
        self.assertIn("Neue Anmeldung.", body)
        self.assertIn("Rollen prüfen", body)
        self.assertNotIn("<script", body)
        self.assertEqual(body.count("Neue Anmeldung."), 1)
        self.assertIn("Fix login", body)
        self.assertIn("Add tests", body)
        self.assertEqual(sorted(self._prs().mapped("number")), [77, 78])
        # the entry comes before the PR lines
        self.assertLess(body.index("Neue Anmeldung."), body.index("Fix login"))

    def test_summary_with_release_log_is_deduplicated_on_replay(self):
        payload = self._payload(release_log=self._release_log())
        self._post(payload)
        before = len(self.ticket.message_ids)
        self.assertEqual(self._post(payload).status_code, 200)
        self.ticket.invalidate_recordset()
        self.assertEqual(len(self.ticket.message_ids), before)

    def test_summary_without_release_log_keeps_the_legacy_shape(self):
        resp = self._post(self._payload())
        self.assertEqual(resp.status_code, 200)
        self.ticket.invalidate_recordset()
        body = self.ticket.message_ids[:1].body
        self.assertNotIn("Release ", body)
        self.assertIn("Session handling fixed.", body)
```

In `tests/test_contracts.py::test_change_summary_sample_is_accepted` add, after the existing assertions:
```python
        body = self.ticket.message_ids[:1].body
        self.assertIn(payload["release_log"]["title"], body)
        self.assertIn("Neue Anmeldung", body)
```

- [ ] **Step 2: Run them to verify they fail**

Test command (log to a file; one Odoo on port 8169 at a time):
```
/home/joseph/Projects/Cloudunify/ast-odoo/.venv/bin/python /home/joseph/Projects/Cloudunify/ast-odoo/odoo/odoo-bin -d cu_reva_test --db_host=/run/postgresql --addons-path=/home/joseph/Projects/Cloudunify/Cloudunify/custom_addons,/home/joseph/Projects/Cloudunify/Cloudunify/3rd_party_addons,/home/joseph/Projects/Cloudunify/ast-odoo/enterprise,/home/joseph/Projects/Cloudunify/ast-odoo/odoo/addons -u cu_reva_ticket_analysis --test-enable --test-tags /cu_reva_ticket_analysis:TestRevaChangeSummaryCallback,/cu_reva_ticket_analysis:TestTicketCallbackContracts --stop-after-init --http-port=8169 --log-level=warn
```
Expected: the three new tests and the contract test fail (block ignored).

- [ ] **Step 3: Router**

```python
class ReleaseLogBlock(BaseModel):
    """The ticket's entry in the repo's release log, rendered by REVA to simple
    HTML; posted once above the PR lines when present."""

    release: str
    ticket: int
    title: str
    status: str
    modules: list[str] = Field(default_factory=list)
    html: str


class ChangeSummaryRequest(BaseModel):
    ticket_id: int
    model_name: str
    notes: list[ChangeSummaryNote] = Field(min_length=1)
    release_log: ReleaseLogBlock | None = None
```
Route: `record._apply_reva_change_summary([note.model_dump() for note in body.notes], release_log=body.release_log.model_dump() if body.release_log else None)`.

- [ ] **Step 4: Model**

`_apply_reva_change_summary(self, notes: list, release_log: dict | None = None) -> bool`:
- Dedup key: `payload_hash = hashlib.sha256(json.dumps(notes if release_log is None else {"notes": notes, "release_log": release_log}, sort_keys=True).encode()).hexdigest()` (a legacy payload keeps its previous hash, so replays across the upgrade still dedup).
- After the header line and before the notes loop:
```python
        if release_log:
            caption = Markup("<p><strong>{ticket} — {title}</strong> · Release {release} · {status}{modules}</p>").format(
                ticket=release_log["ticket"],
                title=release_log["title"],
                release=release_log["release"],
                status=release_log["status"],
                modules=(" · " + ", ".join(release_log.get("modules") or [])) if release_log.get("modules") else "",
            )
            body += caption
            body += Markup("{}").format(Markup(html_sanitize(release_log["html"])))  # nosec B704 - sanitized by html_sanitize above
```
- In the loop, keep the PR line; append the note only when `note["note_html"]` is non-empty (`safe_note` computed only then).
- Docstring: one sentence on the block.

- [ ] **Step 5: Version and docs**

`__manifest__.py` 19.0.55.3.0; `CLAUDE.md` version line and, in the router bullet, extend the `/change-summary` sentence: "…; since 19.0.55.3.0 an optional `release_log` block (the ticket's entry from the repo's `docs/releases/<name>.md`, rendered by REVA) is posted once above the PR lines and the PR notes may be empty". `README.md` "Batched changes merged note" paragraph: add two sentences: when the repository's release log has an entry for the ticket, REVA sends that entry (Gebaut and To-do, as the developer wrote it) instead of drafted per-PR text, and the note shows it once with the merged PRs listed beneath; tickets without an entry keep the drafted notes.

- [ ] **Step 6: Run the tests, then the module suite**

The two classes above, then the full module (`--test-tags /cu_reva_ticket_analysis`; baseline: 10 unrelated sale-line failures, `TestGroupRelease` ×2, `TestCuRelease` ×4, `TestSaleLineBanner` ×4). Format with `/home/joseph/.local/bin/pre-commit run --files <files>` (prettier cannot start here; bandit B704/B106 are pre-existing).

```bash
cd /home/joseph/Projects/Cloudunify/Cloudunify && git add custom_addons/cu_reva_ticket_analysis/routers/reva_router.py custom_addons/cu_reva_ticket_analysis/models/reva_mixin.py custom_addons/cu_reva_ticket_analysis/tests/test_callback.py custom_addons/cu_reva_ticket_analysis/tests/test_contracts.py custom_addons/cu_reva_ticket_analysis/__manifest__.py custom_addons/cu_reva_ticket_analysis/CLAUDE.md custom_addons/cu_reva_ticket_analysis/README.md reva_contracts custom_addons/cu_reva_connector/tests/test_contracts.py
```

---

### Task 6: REVA docs and archive

**Files:** `HANDOFF.md` (addendum), `README.md` (the change-notes sentence in "Support answers"/ticket loop section: search for "change note"), `docs/superpowers/specs/2026-09-04-release-log-change-notes-design.md` + this plan → `archive/`.

- [ ] Add to `HANDOFF.md`, above the previous addendum: "## Addendum 2026-09-04 — release-log entries replace drafted change notes": what changed (source column, migration 049, lookup, `release_log` block, Odoo 19.0.55.3.0), deploy (migration 049 at boot, worker/api rebuild), owed (first live ticket-ready summary with a covered ticket).
- [ ] README: one sentence where change notes are described: a ticket covered by the repo's release log gets its entry instead of a drafted note.
- [ ] Move spec + plan to `archive/`, fix the `Spec:` path in the plan header, stage: `git add HANDOFF.md README.md docs/superpowers/specs/archive/2026-09-04-release-log-change-notes-design.md docs/superpowers/plans/archive/2026-09-04-release-log-change-notes.md` (use `mv` for the untracked files).
- [ ] Final: `make test && ruff check reva worker/worker api/app scheduler/scheduler`; `git status --short`.
