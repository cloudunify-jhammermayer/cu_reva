# Release log lookup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On Odoo's *Request Release Note*, REVA finds the customer repo's committed `docs/releases/<slug>.html`, hands Odoo its docs-site URL, the fragment and a shared theme CSS (or a German failure reason), styles those fragments in the docs site, and names the release on GitHub issues it creates.

**Architecture:** One SCSS theme in `docs-ui/` compiled into `reva/static/release-log.css` (packaged with the shared library so the worker can send it). A new instance-gated route `POST /api/v1/release-note` inserts a `release_notes` row and enqueues `worker.release_note_tasks.run_release_note`; the runner fetches `.claude-review.yml` from every enabled repo, keeps those declaring `odoo_instance: <name>`, reads `docs/releases/<slug>.html` from their default branch and posts `/releases/release-note` to Odoo. No Claude call. Everything mirrors the timesheet-review route/runner/writers pattern.

**Tech Stack:** Python 3.14 (FastAPI `api/`, RQ `worker/`, shared `reva/`), SQLAlchemy + plain SQL migration, pytest with SQLite in-memory; Vue 3 + Vite + `sass` (`docs-ui/`); Go Bubble Tea (`tui/`).

**Spec:** `docs/superpowers/specs/2026-09-04-release-log-requirements.md` (R1 theme, R2 lookup, R3 Odoo side for reference only, R4 release line on issues). Counterpart: `../wenatex_odoo/docs/superpowers/specs/2026-09-04-release-log-and-overview-design.md` (the generator that emits the `rl-*` fragment).

## Global Constraints

- **No commits.** Joseph commits himself. Every task ends with `git add` staging only; never run `git commit` or `git push`. Files changed in `../Cloudunify` (Task 6) are left unstaged there too.
- **No Claude call anywhere in this feature.** No budget gate, no cost columns, no prompt.
- **Slug rule (spec R2):** `name.strip().lower()` with every whitespace run replaced by `-`; page path `docs/releases/<slug>.html`. Nothing else is normalised (umlauts stay).
- **German error texts verbatim (spec R2):** `Kein Release-Log 'docs/releases/<slug>.html' in <repo list>` (repos as `owner/name`, comma-separated, ascending id) and `` Kein Repository mit `odoo_instance: <instance name>` in .claude-review.yml ``.
- **Callback fields (spec R2):** `{release_id, note_id, status, url, html, css, error}`; `url`/`html`/`css` on `completed`, `error` on `failed`. Path `/releases/release-note`.
- **Odoo contract stays untouched:** 202 with `note_id` within 10 s; the callback tolerates 401/404/409 as terminal, 5xx as retry; Odoo's 30-minute watchdog is the backstop.
- **Ops events (spec R2):** every caught-and-degraded path calls `writers.record_ops_event`; a missing release log is an outcome and gets no ops event.
- **Definition of done per Python task:** `make test` (a shared `reva/` change affects worker, api and scheduler) and `ruff check reva worker/worker api/app scheduler/scheduler`. Go task: `cd tui && go build ./... && go vet ./... && go test ./...`. Contract task: `python -m reva.odoo_contracts generate` and `scripts/sync_contracts.sh ../Cloudunify`.
- **Style:** match the neighbouring file (timesheet route/runner/writers); comments and docs in English; plain ASCII numbering in docs; no new abstractions, no adjacent refactoring.
- Python tests build tables from the ORM models, not from `db/migrations/*.sql`; the migration's SQL is only exercised on real Postgres (`make test-integration` or first staging boot). Say so in the handoff.

---

### Task 1: Release-log theme (docs-ui) compiled into `reva/static/release-log.css`

**Files:**
- Modify: `docs-ui/package.json` (devDependencies + `build:theme` script)
- Create: `docs-ui/src/release-log.scss`
- Modify: `docs-ui/src/main.js:4` (import after `style.css`)
- Create: `reva/static/release-log.css` (generated, committed)
- Modify: `pyproject.toml` (package data)
- Modify: `docs-ui/README.md` ("Build & deploy" section)

**Interfaces:**
- Consumes: the `rl-*` markup contract from the spec (R1 class list) and the wenatex generator (`article.rl-ticket[data-status]`, `div.rl-stat[data-st]` with values `umgesetzt|weitgehend|teilweise|offen|todo`).
- Produces: `reva/static/release-log.css`, read by `reva.release_log.theme_css()` (Task 2). Selectors `.rl-page` (light tokens) and `.markdown-body .rl-page` (dark tokens).

- [ ] **Step 1: Add `sass` and the theme script**

Run:
```bash
cd docs-ui && npm install --save-dev sass@^1.104.0
```
Then edit `docs-ui/package.json` so `scripts` reads:
```json
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "build:theme": "sass src/release-log.scss ../reva/static/release-log.css --no-source-map",
    "preview": "vite preview"
  },
```
`package-lock.json` changes with the install; it is part of the change.

- [ ] **Step 2: Write the theme**

Create `docs-ui/src/release-log.scss`:

```scss
// Release-log theme (spec docs/superpowers/specs/2026-09-04-release-log-requirements.md, R1).
//
// Styles the `rl-*` fragment the customer repos' docs generator commits as
// docs/releases/<name>.html. The fragment carries no <style> (the docs site's
// sanitizer strips it), so the look lives here, once, for every project:
//   - `.rl-page` carries the light token set: what Odoo's iframe shows.
//   - `.markdown-body .rl-page` switches to the dark set matching the docs
//     site (`--bg-primary #1e1e1e`); the same fragment reads right in both.
// Stripe and pill colours come from the `data-status` attribute, never from
// inline styles. `npm run build:theme` compiles this file to
// reva/static/release-log.css, which the worker sends to Odoo verbatim.
// Rules are scoped under .rl-* so the site's `.markdown-body` defaults
// (heading borders, link colour) do not leak into the cards.

@mixin rl-light {
  --rl-ground: #fbfbf9;
  --rl-panel: #f1f2ef;
  --rl-line: #dfe2dd;
  --rl-line-strong: #c3c8c1;
  --rl-ink: #1f2422;
  --rl-muted: #666d69;
  --rl-accent: #0f4c5c;
  --rl-accent-ink: #0b3a46;
  --rl-pill-ink: #ffffff;
  --st-umgesetzt: #1f7a4d;
  --st-weitgehend: #5a8f2b;
  --st-teilweise: #b7791f;
  --st-offen: #8b918d;
}

@mixin rl-dark {
  --rl-ground: #1e1e1e;
  --rl-panel: #262626;
  --rl-line: #363636;
  --rl-line-strong: #4a4a4a;
  --rl-ink: #e8e9e6;
  --rl-muted: #a3a8a4;
  --rl-accent: #5fb3c4;
  --rl-accent-ink: #86c9d6;
  --rl-pill-ink: #131414;
  --st-umgesetzt: #3fae78;
  --st-weitgehend: #86bf58;
  --st-teilweise: #d9a441;
  --st-offen: #858b87;
}

.rl-page {
  @include rl-light;
  background: var(--rl-ground);
  color: var(--rl-ink);
  font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
  font-size: 16px;
  line-height: 1.5;
  max-width: 1120px;
  margin: 0 auto;
  padding: 32px 24px 56px;

  h1, h2, h3 { margin: 0; color: var(--rl-ink); border-bottom: 0; padding-bottom: 0; }
  h1 {
    font-size: clamp(26px, 3.4vw, 36px); font-weight: 300; line-height: 1.15;
    text-transform: uppercase; letter-spacing: 0.05em;
  }
  h1 strong, h1 b { font-weight: 600; }
  h2 {
    display: inline-block; font-size: 15px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.05em; padding-bottom: 10px; border-bottom: 3px solid var(--rl-accent);
  }
  a { color: var(--rl-accent-ink); }
  p { margin: 0; }
  ul { margin: 0; padding-left: 18px; }
  li { margin: 2px 0; }
  li em { color: var(--rl-muted); font-style: normal; }
  code {
    font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 0.9em;
    background: var(--rl-panel); padding: 1px 5px; border-radius: 2px;
  }
}

.markdown-body .rl-page { @include rl-dark; }

// Masthead
.rl-masthead {
  display: grid; grid-template-columns: 1fr auto; gap: 24px 40px; align-items: end;
  padding-bottom: 24px; border-bottom: 1px solid var(--rl-line-strong);
  p { margin: 12px 0 0; max-width: 62ch; color: var(--rl-muted); }
}
.rl-eyebrow {
  font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.12em;
  color: var(--rl-muted); margin: 0 0 14px;
  b { color: var(--rl-accent); }
}
.rl-meta {
  display: grid; grid-template-columns: auto auto; gap: 4px 18px; font-size: 14px;
  color: var(--rl-muted); white-space: nowrap; margin: 0;
  dt { font-weight: 600; color: var(--rl-ink); }
  dd { margin: 0; font-variant-numeric: tabular-nums; }
  a { text-decoration: none; border-bottom: 1px solid transparent; }
  a:hover, a:focus-visible { border-bottom-color: currentColor; outline: none; }
}

// Stats
.rl-stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  border: 1px solid var(--rl-line); margin: 28px 0 44px; background: var(--rl-panel);
}
.rl-stat { padding: 16px 18px 14px; border-left: 1px solid var(--rl-line); display: grid; gap: 2px; }
.rl-stat:first-child { border-left: 0; }
.rl-stat-num { font-size: 30px; font-weight: 600; line-height: 1; font-variant-numeric: tabular-nums; }
.rl-stat-label {
  font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: var(--rl-muted);
}
.rl-stat[data-st="umgesetzt"] .rl-stat-num { color: var(--st-umgesetzt); }
.rl-stat[data-st="weitgehend"] .rl-stat-num { color: var(--st-weitgehend); }
.rl-stat[data-st="teilweise"] .rl-stat-num { color: var(--st-teilweise); }
.rl-stat[data-st="offen"] .rl-stat-num { color: var(--st-offen); }
.rl-stat[data-st="todo"] .rl-stat-num { color: var(--rl-accent); }

// Ticket cards
.rl-tickets { display: grid; gap: 12px; }
.rl-ticket {
  --st: var(--st-offen);
  display: grid; grid-template-columns: 6px 170px 1fr 1fr; gap: 0 22px;
  border: 1px solid var(--rl-line); background: var(--rl-ground);
  h3 { font-size: 15px; font-weight: 600; letter-spacing: 0.01em; grid-column: 1 / -1; }
  p { font-size: 15px; }
  ul { font-size: 14.5px; }
}
.rl-ticket[data-status="umgesetzt"] { --st: var(--st-umgesetzt); }
.rl-ticket[data-status="weitgehend"] { --st: var(--st-weitgehend); }
.rl-ticket[data-status="teilweise"] { --st: var(--st-teilweise); }
.rl-ticket[data-status="offen"] { --st: var(--st-offen); }
.rl-stripe { background: var(--st); }
.rl-head { padding: 16px 0 16px 16px; display: grid; gap: 8px; align-content: start; }
.rl-id {
  font-size: 20px; font-weight: 600; line-height: 1; font-variant-numeric: tabular-nums;
  small {
    display: block; font-size: 11px; font-weight: 600; letter-spacing: 0.06em;
    color: var(--rl-muted); margin-top: 6px;
  }
}
.rl-pill {
  justify-self: start; font-size: 10.5px; font-weight: 700; letter-spacing: 0.08em;
  text-transform: uppercase; padding: 4px 8px; color: var(--rl-pill-ink); background: var(--st);
  border-radius: 2px;
}
.rl-col { padding: 16px 0; display: grid; gap: 6px; align-content: start; }
.rl-col:last-child { padding-right: 16px; }
.rl-label {
  font-size: 10.5px; font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase; color: var(--rl-muted);
}
.rl-built { display: grid; gap: 6px; }
.rl-todo ul { color: var(--rl-ink); }
.rl-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
.rl-chip {
  font-size: 11.5px; font-weight: 600; padding: 3px 7px; background: var(--rl-panel);
  color: var(--rl-ink); border: 1px solid var(--rl-line); text-decoration: none; border-radius: 2px;
}
.rl-chip:hover, .rl-chip:focus-visible { border-color: var(--rl-accent); color: var(--rl-accent-ink); outline: none; }

// Footer notes (Entscheidungen / Nicht in diesem Release); empty when none
.rl-notes {
  margin-top: 44px; padding-top: 20px; border-top: 1px solid var(--rl-line-strong);
  display: grid; grid-template-columns: 1fr 1fr; gap: 24px 40px; font-size: 14.5px; color: var(--rl-muted);
}
.rl-notes:empty { display: none; }
.rl-note {
  h2 { font-size: 12px; border-bottom-width: 2px; margin-bottom: 8px; }
  li { margin: 3px 0; }
  li strong { color: var(--rl-ink); font-weight: 600; }
}

@media (max-width: 860px) {
  .rl-masthead { grid-template-columns: 1fr; align-items: start; }
  .rl-ticket { grid-template-columns: 6px 1fr; }
  .rl-head { padding-bottom: 0; }
  .rl-col { padding: 12px 16px 0 16px; }
  .rl-col:last-child { padding-bottom: 16px; }
  .rl-notes { grid-template-columns: 1fr; }
}

// Print / PDF export: the docs site forces a white page on .markdown-body
// (style.css @media print), so the dark tokens must flip back to the light set
// or the cards print dark on white. One column keeps a card on one page.
@media print {
  .rl-page, .markdown-body .rl-page { @include rl-light; background: #fff; }
  .rl-ticket { grid-template-columns: 6px 1fr; break-inside: avoid; }
  .rl-head { padding-bottom: 0; }
  .rl-col { padding: 10px 14px 0 14px; }
  .rl-col:last-child { padding-bottom: 14px; }
  .rl-stats { grid-template-columns: repeat(2, 1fr); }
  .rl-notes { grid-template-columns: 1fr; }
  .rl-chip { border-color: #999; }
}
```

- [ ] **Step 3: Import it into the SPA**

`docs-ui/src/main.js` becomes:
```js
import { createApp } from 'vue'
import App from './App.vue'
import 'highlight.js/styles/github-dark.css'
import './style.css'
import './release-log.scss'
```

- [ ] **Step 4: Build both outputs**

Run:
```bash
cd docs-ui && npm run build && npm run build:theme
```
Expected: `vite build` succeeds (the SCSS compiles into the bundle) and `reva/static/release-log.css` exists. Check:
```bash
cd .. && test -s reva/static/release-log.css && grep -c '\.rl-ticket\[data-status=umgesetzt\]' reva/static/release-log.css && grep -c '\.markdown-body \.rl-page' reva/static/release-log.css && git check-ignore reva/static/release-log.css; echo "ignored? exit=$?"
```
Expected: both greps print `1`; `git check-ignore` prints nothing and exits 1 (the file is not ignored).

- [ ] **Step 5: Determinism check (spec R1 acceptance)**

Run `cd docs-ui && npm run build:theme && cd .. && git status --short reva/static/` twice. Expected: the second run changes nothing (same `??`/`M` line, no new diff).

- [ ] **Step 6: Package the CSS with the shared library**

Append to `pyproject.toml`:
```toml

[tool.setuptools.package-data]
reva = ["static/*.css"]
```
Verify the wheel would carry it:
```bash
worker/.venv/bin/pip install --no-deps --force-reinstall --quiet . && worker/.venv/bin/python -c "from importlib import resources; print(len((resources.files('reva') / 'static' / 'release-log.css').read_text()))"
```
Expected: a positive number. Then restore the editable install the tests rely on: `worker/.venv/bin/pip install --no-deps --quiet -e .`.

- [ ] **Step 7: Document the theme build**

In `docs-ui/README.md`, after the line `To build just the static assets locally: \`npm run build\` → \`dist/\`.` add:

```markdown

### Release-log theme

`src/release-log.scss` styles the `rl-*` release-log fragments customer repos
commit under `docs/releases/`. It is bundled into the SPA by `npm run build`
**and** compiled standalone by `npm run build:theme` into
`../reva/static/release-log.css`, which the worker sends to Odoo with every
release-log callback. Run `build:theme` after every SCSS change and commit the
CSS; the nginx image build does not run it.
```

- [ ] **Step 8: Stage**

```bash
git add docs-ui/package.json docs-ui/package-lock.json docs-ui/src/release-log.scss docs-ui/src/main.js docs-ui/README.md reva/static/release-log.css pyproject.toml
```
Visual acceptance (cards, pills, stats, dark theme on a real `docs/releases/lollipop.html`) needs a registered repo carrying the page; it is owed on staging and recorded in the handoff (Task 9).

---

### Task 2: Shared library: slug, URL, theme reader, config, types

**Files:**
- Modify: `reva/config.py` (append after the `GOLDEN_ESTIMATE_LIMIT` block, before `def env_or_file`)
- Create: `reva/release_log.py`
- Modify: `reva/types.py:57-110` (`RepoConfig` gains `odoo_instance`), `reva/types.py:877` (`ReleaseRef` above `TicketIssueJobParams`, `release` field on it), end of file (`ReleaseNoteJobParams`)
- Modify: `reva/README.md:28` (module table row after `repo_docs.py`)
- Modify: `README.md:135` (`.claude-review.yml` key table), `CLAUDE.md` "Scope filtering" bullet (key list)
- Test: `worker/tests/test_release_log.py` (new)

**Interfaces:**
- Consumes: `reva/static/release-log.css` (Task 1).
- Produces:
  - `reva.config.DOCS_SITE_URL: str` (env `REVA_DOCS_SITE_URL`, stripped, no trailing slash, `""` when unset)
  - `reva.release_log.release_slug(name: str) -> str`
  - `reva.release_log.release_log_path(slug: str) -> str` (`docs/releases/<slug>.html`)
  - `reva.release_log.docs_site_page_url(repository_id: int, path: str) -> str`
  - `reva.release_log.theme_css() -> str`
  - `reva.types.RepoConfig.odoo_instance: str | None = None`
  - `reva.types.ReleaseRef(id: int, name: str = "", date: str | None = None)`
  - `reva.types.TicketIssueJobParams.release: ReleaseRef | None = None`
  - `reva.types.ReleaseNoteJobParams(note_id: int, odoo_instance_id: int, release_id: int, release_name: str, slug: str)`

- [ ] **Step 1: Write the failing tests**

Create `worker/tests/test_release_log.py`:

```python
"""Tests for reva.release_log and the release-log types (spec 2026-09-04, R2/R4)."""

from __future__ import annotations

import pytest

from reva import config, release_log
from reva.types import ReleaseNoteJobParams, ReleaseRef, RepoConfig, TicketIssueJobParams


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Lollipop", "lollipop"),
        ("  lollipop  ", "lollipop"),
        ("Big Bang 2", "big-bang-2"),
        ("Tab\tand  double  space", "tab-and-double-space"),
        ("Ünïcode", "ünïcode"),
    ],
)
def test_release_slug(name, slug):
    assert release_log.release_slug(name) == slug


def test_release_log_path():
    assert release_log.release_log_path("lollipop") == "docs/releases/lollipop.html"


def test_docs_site_page_url_uses_configured_base(monkeypatch):
    monkeypatch.setattr(config, "DOCS_SITE_URL", "https://reva.example.com")
    assert (
        release_log.docs_site_page_url(7, "docs/releases/lollipop.html")
        == "https://reva.example.com/docs/?repo=7&path=docs/releases/lollipop.html"
    )


def test_docs_site_page_url_is_relative_when_unset(monkeypatch):
    monkeypatch.setattr(config, "DOCS_SITE_URL", "")
    assert (
        release_log.docs_site_page_url(7, "docs/releases/lollipop.html")
        == "/docs/?repo=7&path=docs/releases/lollipop.html"
    )


def test_theme_css_is_packaged():
    css = release_log.theme_css()
    assert ".rl-page" in css
    # dart-sass unquotes identifier-valued attribute selectors in its output.
    assert ".rl-ticket[data-status=umgesetzt]" in css
    assert "<style" not in css


def test_repo_config_odoo_instance_key():
    assert RepoConfig().odoo_instance is None
    assert RepoConfig.model_validate({"odoo_instance": "wenatex"}).odoo_instance == "wenatex"


def test_release_ref_rides_ticket_issue_params():
    params = TicketIssueJobParams(
        run_id=1, odoo_instance_id=1, ticket_id=42, model_name="project.task",
        github_url="https://github.com/acme/widgets", name="Login", description="",
        analysis_html="", priority="1", ticket_url="https://odoo.example.com/web#id=42",
        release={"id": 3275, "name": "Lollipop", "date": "2026-09-30 00:00:00"},
    )
    assert params.release == ReleaseRef(id=3275, name="Lollipop", date="2026-09-30 00:00:00")
    again = TicketIssueJobParams.model_validate(params.model_dump())
    assert again.release == params.release
    assert TicketIssueJobParams.model_validate({**params.model_dump(), "release": None}).release is None


def test_release_note_job_params_roundtrip():
    p = ReleaseNoteJobParams(
        note_id=1, odoo_instance_id=1, release_id=3275, release_name="Lollipop", slug="lollipop"
    )
    assert ReleaseNoteJobParams.model_validate(p.model_dump()) == p
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_release_log.py -q`
Expected: ImportError (`reva.release_log` does not exist).

- [ ] **Step 3: Config constant**

In `reva/config.py`, after the `GOLDEN_ESTIMATE_LIMIT` try/except block and before `def env_or_file`, add:

```python
# Public base URL of the docs site (nginx serves the SPA under /docs/), used to
# build the release-log page link sent to Odoo (spec 2026-09-04-release-log,
# R2). Empty = unset: the worker still sends a relative link and records an ops
# event, so a missing value is visible on the first request, not silent.
DOCS_SITE_URL = os.environ.get("REVA_DOCS_SITE_URL", "").strip().rstrip("/")
```

- [ ] **Step 4: The helper module**

Create `reva/release_log.py`:

```python
"""Release-log lookup helpers (spec docs/superpowers/specs/2026-09-04-release-log-requirements.md).

Customer repos commit `docs/releases/<slug>.html`, an HTML fragment with
`rl-*` classes generated from the developer-written Markdown next to it. REVA
does not draft anything: on Odoo's request it finds that page and ships it with
the shared theme (`static/release-log.css`, compiled from
`docs-ui/src/release-log.scss` by `npm run build:theme`).
"""

from __future__ import annotations

import re
from importlib import resources
from urllib.parse import quote

from reva import config

RELEASE_DIR = "docs/releases"
_WHITESPACE = re.compile(r"\s+")


def release_slug(name: str) -> str:
    """`name.strip().lower()` with each whitespace run replaced by `-` (R2).
    Nothing else is normalised: the generator on the repo side applies the
    same rule, and both must agree byte for byte."""
    return _WHITESPACE.sub("-", name.strip().lower())


def release_log_path(slug: str) -> str:
    return f"{RELEASE_DIR}/{slug}.html"


def docs_site_page_url(repository_id: int, path: str) -> str:
    """Docs-site deep link (`docs-ui/src/location.js`: `?repo=<id>&path=<file>`,
    no `ref` = default branch). Relative when REVA_DOCS_SITE_URL is unset."""
    return f"{config.DOCS_SITE_URL}/docs/?repo={repository_id}&path={quote(path, safe='/')}"


def theme_css() -> str:
    """The compiled theme, packaged with the library so every service image
    (the worker sends it) carries the same bytes the docs site renders with."""
    return (resources.files("reva") / "static" / "release-log.css").read_text(encoding="utf-8")
```

- [ ] **Step 5: Types**

In `reva/types.py`, inside `class RepoConfig` after the `odoo_version` field declaration (before its `@field_validator`), add:

```python
    # Name of the Odoo instance (odoo_instances.name) whose release-log lookups
    # search this repo for docs/releases/<slug>.html. Unset: never searched.
    odoo_instance: str | None = None
```

Directly above `class TicketIssueJobParams(BaseModel):` add:

```python
class ReleaseRef(BaseModel):
    """The Odoo `cu.release` a record ships in. Optional block on the ticket
    calls (handoff 2026-09-01, leg 3); only `name` is used, on created issues."""

    id: int
    name: str = ""
    # "YYYY-MM-DD HH:MM:SS" (UTC) or None as Odoo sends it; passed through, never parsed.
    date: str | None = None


```

Inside `class TicketIssueJobParams`, after `plan_date: date | None = None`, add:

```python
    # Release the ticket ships in (R4): named on every created issue body.
    release: ReleaseRef | None = None
```

At the end of `reva/types.py` add:

```python


# --- Release-log lookup types (spec 2026-09-04) -------------------------------


class ReleaseNoteJobParams(BaseModel):
    """Inputs of the release-log lookup job. `note_id` is the release_notes row
    id Odoo stores and echoes on the callback; `slug` is derived once in the API
    (`reva.release_log.release_slug`) so worker and TUI see the same value."""

    note_id: int
    odoo_instance_id: int
    release_id: int
    release_name: str
    slug: str
```

- [ ] **Step 6: Run the tests**

Run: `cd worker && .venv/bin/python -m pytest tests/test_release_log.py -q`
Expected: all pass.

- [ ] **Step 7: Docs for the new config key and module**

`reva/README.md`: after the `repo_docs.py` row add
```markdown
| `release_log.py` | Release-log lookup helpers: release slug (`release_slug`), page path under `docs/releases/`, docs-site deep link (`REVA_DOCS_SITE_URL`) and the packaged theme CSS (`theme_css`, from `static/release-log.css`). |
```

`README.md`: in the `.claude-review.yml` key table (the row `| \`odoo\` / \`custom_instructions\` | ...` near line 135) add after it:
```markdown
| `odoo_instance` | — | Name of the Odoo instance (as registered in REVA) whose release-log lookups search this repo's `docs/releases/`. Unset: never searched. |
```

`CLAUDE.md`, "Scope filtering" bullet: append `odoo_instance` to the parenthesised key list after `custom_instructions`.

- [ ] **Step 8: Full check and stage**

Run: `make test && ruff check reva worker/worker api/app scheduler/scheduler`
Expected: green.

```bash
git add reva/config.py reva/release_log.py reva/types.py reva/README.md README.md CLAUDE.md worker/tests/test_release_log.py
```

---

### Task 3: `release_notes` table, model and writers

**Files:**
- Create: `db/migrations/048_release_notes.sql`
- Modify: `reva/db/models.py` (new `ReleaseNote` after `TimesheetReviewLine`, before the `odoo_instances` section)
- Modify: `reva/db/writers.py` (new section after `record_timesheet_callback_sent`, before `# --- ticket_issue_runs writers`)
- Test: `worker/tests/test_release_note_writers.py` (new)

**Interfaces:**
- Produces:
  - `reva.db.models.ReleaseNote`
  - `writers.record_release_note_created(db, *, odoo_instance_id: int, release_id: int, release_name: str, slug: str) -> int`
  - `writers.attach_release_note_job_id(db, note_id: int, job_id: str) -> None`
  - `writers.get_release_note(db, note_id: int) -> dict | None` with keys `id, job_id, odoo_instance_id, release_id, release_name, slug, status, source_repo_id, source_path, url, error, created_at, completed_at, callback_sent_at`
  - `writers.record_release_note_completed(db, note_id: int, *, source_repo_id: int, source_path: str, url: str) -> None` (sets `status="completed"`, `completed_at` and `callback_sent_at`)
  - `writers.record_release_note_failed(db, note_id: int, error: str) -> None`
  - `writers.record_release_note_callback_sent(db, note_id: int) -> None`
  - `writers.list_enabled_repositories(db) -> list[dict]` with keys `id, owner, name, full_name, default_branch, installation_id`, ascending `id`

- [ ] **Step 1: Write the failing tests**

Create `worker/tests/test_release_note_writers.py`:

```python
"""Tests for the release_notes writers (migration 048, spec 2026-09-04 R2)."""

from __future__ import annotations

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import Repository


@pytest.fixture()
def db():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _create(db, name="Lollipop", slug="lollipop"):
    return writers.record_release_note_created(
        db, odoo_instance_id=1, release_id=3275, release_name=name, slug=slug
    )


def test_created_row_is_pending(db):
    note_id = _create(db)
    writers.attach_release_note_job_id(db, note_id, "rq:job:1")
    row = writers.get_release_note(db, note_id)
    assert row["status"] == "pending"
    assert row["job_id"] == "rq:job:1"
    assert (row["odoo_instance_id"], row["release_id"]) == (1, 3275)
    assert (row["release_name"], row["slug"]) == ("Lollipop", "lollipop")
    assert row["source_repo_id"] is None and row["url"] is None and row["error"] is None
    assert row["completed_at"] is None and row["callback_sent_at"] is None


def test_completed_sets_source_and_both_timestamps(db):
    note_id = _create(db)
    writers.record_release_note_completed(
        db, note_id, source_repo_id=4, source_path="docs/releases/lollipop.html",
        url="https://reva.example.com/docs/?repo=4&path=docs/releases/lollipop.html",
    )
    row = writers.get_release_note(db, note_id)
    assert row["status"] == "completed"
    assert row["source_repo_id"] == 4
    assert row["source_path"] == "docs/releases/lollipop.html"
    assert row["url"].endswith("lollipop.html")
    assert row["completed_at"] is not None and row["callback_sent_at"] is not None


def test_failed_keeps_error_and_marks_callback_separately(db):
    note_id = _create(db)
    writers.record_release_note_failed(
        db, note_id, "Kein Release-Log 'docs/releases/lollipop.html' in acme/widgets"
    )
    row = writers.get_release_note(db, note_id)
    assert row["status"] == "failed" and row["error"].startswith("Kein Release-Log")
    assert row["completed_at"] is not None and row["callback_sent_at"] is None
    writers.record_release_note_callback_sent(db, note_id)
    assert writers.get_release_note(db, note_id)["callback_sent_at"] is not None


def test_get_release_note_missing(db):
    assert writers.get_release_note(db, 999) is None


def test_list_enabled_repositories_orders_by_id_and_skips_disabled(db):
    with db.session() as s:
        s.add(Repository(id=2, github_repository_id=1002, owner="acme", name="second",
                         full_name="acme/second", installation_id=7, enabled=True))
        s.add(Repository(id=1, github_repository_id=1001, owner="acme", name="first",
                         full_name="acme/first", installation_id=7, enabled=True,
                         default_branch="develop"))
        s.add(Repository(id=3, github_repository_id=1003, owner="acme", name="off",
                         full_name="acme/off", installation_id=7, enabled=False))
    repos = writers.list_enabled_repositories(db)
    assert [r["full_name"] for r in repos] == ["acme/first", "acme/second"]
    assert repos[0] == {
        "id": 1, "owner": "acme", "name": "first", "full_name": "acme/first",
        "default_branch": "develop", "installation_id": 7,
    }
    assert repos[1]["default_branch"] == "main"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_release_note_writers.py -q`
Expected: AttributeError on `writers.record_release_note_created`.

- [ ] **Step 3: Migration**

Create `db/migrations/048_release_notes.sql`:

```sql
-- Release-log lookups requested by Odoo (spec 2026-09-04-release-log-requirements, R2).
-- One row per POST /api/v1/release-note; `id` is the note_id Odoo stores and
-- echoes on the callback. No content is stored: the repo's
-- docs/releases/<slug>.html is the source of truth, the row records where it
-- was found and how the exchange ended. `completed` means delivered (the
-- callback was accepted), so completed_at and callback_sent_at are set together.
-- Mirrors reva/db/models.py::ReleaseNote.
CREATE TABLE IF NOT EXISTS release_notes (
    id BIGSERIAL PRIMARY KEY,
    job_id TEXT,
    odoo_instance_id BIGINT NOT NULL REFERENCES odoo_instances(id),
    release_id BIGINT NOT NULL,
    release_name TEXT NOT NULL,
    slug TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    source_repo_id BIGINT REFERENCES repositories(id),
    source_path TEXT,
    url TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    callback_sent_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_release_notes_created
    ON release_notes (created_at DESC);

-- "What did we answer for this release?" (TUI drill-down, support questions).
CREATE INDEX IF NOT EXISTS idx_release_notes_instance_release
    ON release_notes (odoo_instance_id, release_id);
```

- [ ] **Step 4: Model**

In `reva/db/models.py`, after `class TimesheetReviewLine` and before the `# --- odoo_instances` section header, add:

```python
# ----------------------------------------------------------------- release_notes


class ReleaseNote(Base):
    """One Odoo release-log lookup (migration 048). `id` is the note_id Odoo
    echoes on the callback. No content: the repo page is the source of truth,
    the row records where it was found and how the exchange ended."""

    __tablename__ = "release_notes"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(Text)
    odoo_instance_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("odoo_instances.id"), nullable=False
    )
    release_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    release_name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    source_repo_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("repositories.id")
    )
    source_path: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    callback_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_release_notes_created", text("created_at DESC")),
        Index("idx_release_notes_instance_release", "odoo_instance_id", "release_id"),
    )
```

- [ ] **Step 5: Writers**

In `reva/db/writers.py`, add `ReleaseNote` to the `from reva.db.models import (...)` block (keep alphabetical order), then after `record_timesheet_callback_sent` and before `# --- ticket_issue_runs writers` add:

```python
# --- release_notes writers ------------------------------------------------------


def record_release_note_created(
    db: Database, *, odoo_instance_id: int, release_id: int, release_name: str, slug: str
) -> int:
    """Insert a pending release_notes row and return its id (= note_id)."""
    with db.session() as s:
        row = ReleaseNote(
            odoo_instance_id=odoo_instance_id,
            release_id=release_id,
            release_name=release_name,
            slug=slug,
            status="pending",
        )
        s.add(row)
        s.flush()
        return row.id


def attach_release_note_job_id(db: Database, note_id: int, job_id: str) -> None:
    """Store the RQ job ID on the row after enqueuing."""
    with db.session() as s:
        row = s.get(ReleaseNote, note_id)
        if row is not None:
            row.job_id = job_id


def get_release_note(db: Database, note_id: int) -> dict | None:
    """Return a release_notes row as a dict, or None."""
    with db.session() as s:
        row = s.get(ReleaseNote, note_id)
        if row is None:
            return None
        return {
            "id": row.id,
            "job_id": row.job_id,
            "odoo_instance_id": row.odoo_instance_id,
            "release_id": row.release_id,
            "release_name": row.release_name,
            "slug": row.slug,
            "status": row.status,
            "source_repo_id": row.source_repo_id,
            "source_path": row.source_path,
            "url": row.url,
            "error": row.error,
            "created_at": row.created_at,
            "completed_at": row.completed_at,
            "callback_sent_at": row.callback_sent_at,
        }


def record_release_note_completed(
    db: Database, note_id: int, *, source_repo_id: int, source_path: str, url: str
) -> None:
    """Completed means delivered: called after Odoo accepted the callback, so
    completed_at and callback_sent_at are set together. An RQ retry of a
    pending row repeats the cheap lookup; nothing has to be stored to resend."""
    with db.session() as s:
        row = s.get(ReleaseNote, note_id)
        if row is None:
            return
        now = datetime.now(timezone.utc)
        row.status = "completed"
        row.source_repo_id = source_repo_id
        row.source_path = source_path
        row.url = url
        row.completed_at = now
        row.callback_sent_at = now


def record_release_note_failed(db: Database, note_id: int, error: str) -> None:
    """Mark the lookup failed with the reason Odoo is told (German for the
    consultant reading the release chatter, see spec R2)."""
    with db.session() as s:
        row = s.get(ReleaseNote, note_id)
        if row is None:
            return
        row.status = "failed"
        row.error = error
        row.completed_at = datetime.now(timezone.utc)


def record_release_note_callback_sent(db: Database, note_id: int) -> None:
    """The failure callback reached Odoo (best effort; the row is already failed)."""
    with db.session() as s:
        row = s.get(ReleaseNote, note_id)
        if row is not None:
            row.callback_sent_at = datetime.now(timezone.utc)


def list_enabled_repositories(db: Database) -> list[dict]:
    """Every enabled repo, ascending id: the search order and the tie-break for
    ambiguous release-log hits (spec R2)."""
    with db.session() as s:
        rows = s.execute(
            select(Repository).where(Repository.enabled.is_(True)).order_by(Repository.id)
        ).scalars().all()
        return [
            {
                "id": r.id,
                "owner": r.owner,
                "name": r.name,
                "full_name": r.full_name,
                "default_branch": r.default_branch or "main",
                "installation_id": r.installation_id,
            }
            for r in rows
        ]
```

- [ ] **Step 6: Run the tests**

Run: `cd worker && .venv/bin/python -m pytest tests/test_release_note_writers.py -q`
Expected: all pass.

- [ ] **Step 7: Full check and stage**

Run: `make test && ruff check reva worker/worker api/app scheduler/scheduler`
Expected: green.

```bash
git add db/migrations/048_release_notes.sql reva/db/models.py reva/db/writers.py worker/tests/test_release_note_writers.py
```

---

### Task 4: API: `POST /api/v1/release-note` and `GET /api/v1/release-notes`

**Files:**
- Create: `api/app/schemas/release_notes.py`
- Create: `api/app/queries/release_notes.py`
- Create: `api/app/routes/v1/release_notes.py`
- Modify: `api/app/routes/v1/__init__.py` (import + two `include_router` lines)
- Modify: `api/README.md` ("Odoo endpoints" section)
- Test: `api/tests/test_v1_release_notes.py` (new)

**Interfaces:**
- Consumes: `writers.record_release_note_created/attach_release_note_job_id/record_release_note_failed/get_release_note` (Task 3), `reva.release_log.release_slug`, `reva.types.ReleaseNoteJobParams` (Task 2).
- Produces: `ReleaseNoteRequest`, `ReleaseNoteCreated`, `ReleaseNoteSummary`, `ReleaseNotePage`; job enqueued as `"worker.release_note_tasks.run_release_note"` with `ReleaseNoteJobParams.model_dump()`; `app.queries.release_notes.list_release_notes(db, status=None, limit=50, offset=0) -> tuple[list[dict], int]`.

- [ ] **Step 1: Write the failing tests**

Create `api/tests/test_v1_release_notes.py`:

```python
"""Tests for the release-log lookup endpoints (spec 2026-09-04, R2)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers

PAYLOAD = {
    "release_id": 3275,
    "name": "Lollipop",
    "date": "2026-09-30 00:00:00",
    "model_name": "project.task",
    "task_ids": [7595, 7620],
}


@dataclass
class FakeJob:
    id: str


@dataclass
class FakeQueue:
    enqueued: list[tuple] = field(default_factory=list)
    fail: bool = False

    def enqueue(self, func_path, params, **kwargs):
        if self.fail:
            raise RuntimeError("redis down")
        self.enqueued.append((func_path, params, kwargs))
        return FakeJob(id=f"rq:job:fake-{len(self.enqueued)}")


@pytest.fixture()
def client_db_queue(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("REVA_SECRET_KEY", Fernet.generate_key().decode())
    engine = create_engine_from_url(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Database(engine)
    settings = Settings(
        database_url="sqlite:///:memory:",
        github_app_id=1,
        github_webhook_secret="x",
        github_private_key="x",
        redis_url="redis://localhost:6379/0",
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    queue = FakeQueue()
    prev_queue = getattr(app.state, "rq_queue", None)
    app.state.rq_queue = queue
    tc = TestClient(app)
    key = tc.post("/api/v1/odoo-instances", json={
        "name": "wenatex",
        "callback_url": "",
        "callback_api_key": "",
    }).json()["api_key"]
    yield tc, db, queue, {"Authorization": f"Bearer {key}"}
    app.state.rq_queue = prev_queue
    app.dependency_overrides.clear()


def test_submit_enqueues_lookup(client_db_queue):
    client, db, queue, headers = client_db_queue

    r = client.post("/api/v1/release-note", json=PAYLOAD, headers=headers)

    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending"
    func_path, params, kwargs = queue.enqueued[0]
    assert func_path == "worker.release_note_tasks.run_release_note"
    assert params == {
        "note_id": body["note_id"],
        "odoo_instance_id": params["odoo_instance_id"],
        "release_id": 3275,
        "release_name": "Lollipop",
        "slug": "lollipop",
    }
    assert kwargs["retry"] is not None
    assert kwargs["failure_ttl"] == 24 * 3600
    row = writers.get_release_note(db, body["note_id"])
    assert row["job_id"] == body["job_id"]
    assert row["status"] == "pending"


def test_task_ids_optional_and_date_null(client_db_queue):
    client, _, queue, headers = client_db_queue
    payload = {"release_id": 3277, "name": "Marsh Mallow", "date": None,
               "model_name": "project.task", "task_ids": []}

    r = client.post("/api/v1/release-note", json=payload, headers=headers)

    assert r.status_code == 202
    assert queue.enqueued[0][1]["slug"] == "marsh-mallow"


def test_blank_name_is_422(client_db_queue):
    client, _, queue, headers = client_db_queue

    r = client.post("/api/v1/release-note", json={**PAYLOAD, "name": "   "}, headers=headers)

    assert r.status_code == 422
    assert queue.enqueued == []


def test_requires_instance_key(client_db_queue):
    client, *_ = client_db_queue
    assert client.post("/api/v1/release-note", json=PAYLOAD).status_code == 401


def test_queue_down_marks_row_failed_and_503(client_db_queue):
    client, _, queue, headers = client_db_queue
    queue.fail = True

    r = client.post("/api/v1/release-note", json=PAYLOAD, headers=headers)

    assert r.status_code == 503
    listing = client.get("/api/v1/release-notes").json()
    assert listing["total"] == 1
    assert listing["items"][0]["status"] == "failed"
    assert "enqueue failed" in listing["items"][0]["error"]


def test_master_lists_release_notes(client_db_queue):
    client, _, _, headers = client_db_queue
    note_id = client.post("/api/v1/release-note", json=PAYLOAD, headers=headers).json()["note_id"]

    listing = client.get("/api/v1/release-notes").json()

    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["id"] == note_id
    assert (item["release_name"], item["slug"], item["status"]) == ("Lollipop", "lollipop", "pending")
    assert item["url"] is None and item["error"] is None
    assert client.get("/api/v1/release-notes?status=completed").json()["total"] == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd api && .venv/bin/python -m pytest tests/test_v1_release_notes.py -q`
Expected: `404` assertions fail (route not registered) or 405s.

- [ ] **Step 3: Schemas**

Create `api/app/schemas/release_notes.py`:

```python
"""Pydantic schemas for the release-log lookup endpoints (spec 2026-09-04, R2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class ReleaseNoteRequest(BaseModel):
    """What `cu_release._reva_release_note_payload` sends. Only `release_id`
    and `name` drive the lookup; `date`, `model_name` and `task_ids` are
    accepted so the shipped Odoo payload validates, and ignored."""

    release_id: int
    name: str = Field(description="Release name; its slug is the docs/releases/<slug>.html stem")
    date: str | None = Field(
        default=None, description='"YYYY-MM-DD HH:MM:SS" (UTC) or null; not used'
    )
    model_name: str = "project.task"
    task_ids: list[int] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        # A blank name has no page to look up. The 422 reaches the Odoo user as
        # a UserError and rolls the release's pending state back.
        if not v.strip():
            raise ValueError("name must not be blank")
        return v


class ReleaseNoteCreated(BaseModel):
    """202 body. Odoo stores note_id and echoes it on the callback."""

    note_id: int
    job_id: str | None
    status: str


class ReleaseNoteSummary(BaseModel):
    id: int
    odoo_instance_id: int
    release_id: int
    release_name: str
    slug: str
    status: str
    source_repo_id: int | None
    source_path: str | None
    url: str | None
    error: str | None
    callback_sent_at: datetime | None
    created_at: datetime
    completed_at: datetime | None


class ReleaseNotePage(BaseModel):
    items: list[ReleaseNoteSummary]
    total: int
```

- [ ] **Step 4: List query**

Create `api/app/queries/release_notes.py`:

```python
"""Read queries for the release-log lookup endpoints."""

from __future__ import annotations

from sqlalchemy import func, select

from reva.db.engine import Database
from reva.db.models import ReleaseNote


def list_release_notes(
    db: Database,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    with db.session() as s:
        base = select(ReleaseNote)
        count_q = select(func.count()).select_from(ReleaseNote)
        if status:
            base = base.where(ReleaseNote.status == status)
            count_q = count_q.where(ReleaseNote.status == status)

        total = s.execute(count_q).scalar_one()
        rows = s.execute(
            base.order_by(ReleaseNote.created_at.desc(), ReleaseNote.id.desc())
            .limit(limit)
            .offset(offset)
        ).scalars().all()

        items = [
            {
                "id": r.id,
                "odoo_instance_id": r.odoo_instance_id,
                "release_id": r.release_id,
                "release_name": r.release_name,
                "slug": r.slug,
                "status": r.status,
                "source_repo_id": r.source_repo_id,
                "source_path": r.source_path,
                "url": r.url,
                "error": r.error,
                "callback_sent_at": r.callback_sent_at,
                "created_at": r.created_at,
                "completed_at": r.completed_at,
            }
            for r in rows
        ]
    return items, total
```

- [ ] **Step 5: Route**

Create `api/app/routes/v1/release_notes.py`:

```python
"""Release-log lookup endpoints (spec docs/superpowers/specs/2026-09-04-release-log-requirements.md, R2).

POST /api/v1/release-note   — Odoo asks for a release's log page; enqueue the lookup
GET  /api/v1/release-notes  — list lookups for the TUI Releases tab
"""

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from rq import Retry

from app.dependencies import ResolvedOdooInstance, get_db, require_odoo_instance
from app.pagination import clamp_limit, clamp_offset
from app.queries import release_notes as q
from app.schemas.release_notes import (
    ReleaseNoteCreated,
    ReleaseNotePage,
    ReleaseNoteRequest,
    ReleaseNoteSummary,
)
from reva.db import writers
from reva.db.engine import Database
from reva.release_log import release_slug
from reva.types import ReleaseNoteJobParams

router = APIRouter()
create_router = APIRouter()
logger = structlog.get_logger()

# Three retries well inside Odoo's 30-minute watchdog (spec R2). The job is a
# handful of GitHub reads plus one callback, so the timeout is generous.
_RETRY = Retry(max=3, interval=[30, 120, 300])
_FAILURE_TTL = 24 * 3600
_JOB_TIMEOUT = 300


def _enqueue(request: Request, db: Database, note_id: int, params: ReleaseNoteJobParams) -> str:
    rq_queue = request.app.state.rq_queue
    try:
        job = rq_queue.enqueue(
            "worker.release_note_tasks.run_release_note",
            params.model_dump(),
            job_timeout=_JOB_TIMEOUT,
            retry=_RETRY,
            failure_ttl=_FAILURE_TTL,
        )
    except Exception as exc:
        writers.record_release_note_failed(db, note_id, f"enqueue failed: {exc}")
        logger.error("release_note_enqueue_failed", note_id=note_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job queue unavailable; try again",
        ) from exc
    writers.attach_release_note_job_id(db, note_id, job.id)
    return job.id


@create_router.post(
    "/release-note",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ReleaseNoteCreated,
)
def submit_release_note(
    body: ReleaseNoteRequest,
    request: Request,
    db: Database = Depends(get_db),
    instance: ResolvedOdooInstance = Depends(require_odoo_instance),
) -> dict:
    # No budget gate: the lookup makes no paid call.
    slug = release_slug(body.name)
    note_id = writers.record_release_note_created(
        db,
        odoo_instance_id=instance.id,
        release_id=body.release_id,
        release_name=body.name,
        slug=slug,
    )
    params = ReleaseNoteJobParams(
        note_id=note_id,
        odoo_instance_id=instance.id,
        release_id=body.release_id,
        release_name=body.name,
        slug=slug,
    )
    job_id = _enqueue(request, db, note_id, params)
    logger.info("release_note_enqueued", note_id=note_id, job_id=job_id, slug=slug)
    return {"note_id": note_id, "job_id": job_id, "status": "pending"}


@router.get("/release-notes", response_model=ReleaseNotePage)
def list_release_notes(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Database = Depends(get_db),
) -> dict:
    limit = clamp_limit(limit, 200)
    offset = clamp_offset(offset)
    items, total = q.list_release_notes(db, status=status, limit=limit, offset=offset)
    return {
        "items": [ReleaseNoteSummary.model_validate(i) for i in items],
        "total": total,
    }
```

- [ ] **Step 6: Register the routers**

In `api/app/routes/v1/__init__.py`: add `release_notes,` to the `from app.routes.v1 import (...)` list (alphabetical, after `pending`), then add `_master.include_router(release_notes.router)` after the `timesheet_reviews.router` line and `_instance.include_router(release_notes.create_router)` after the `timesheet_reviews.create_router` line.

- [ ] **Step 7: Run the tests**

Run: `cd api && .venv/bin/python -m pytest tests/test_v1_release_notes.py tests/test_contracts_inbound.py -q`
Expected: all pass (the inbound-contract coverage test still passes because no `release-note` contract exists yet; Task 6 adds both sides together).

- [ ] **Step 8: API README**

In `api/README.md`, "Odoo endpoints" section, add after the `GET /api/v1/timesheet-reviews` bullet:

```markdown
- **`POST /api/v1/release-note`** — instance-key-gated request for a release's
  log page (spec `docs/superpowers/specs/2026-09-04-release-log-requirements.md`).
  Creates a pending `release_notes` row and enqueues
  `worker.release_note_tasks.run_release_note`; the worker looks up
  `docs/releases/<slug>.html` in the instance's repos and callbacks Odoo at
  `/releases/release-note` with the docs-site URL, the fragment and the theme
  CSS. No Claude call.
- **`GET /api/v1/release-notes`** — master-key list endpoint consumed by the
  TUI Releases tab: status, source repo/path, URL, error. No page content is
  stored.
```

- [ ] **Step 9: Full check and stage**

Run: `make test && ruff check reva worker/worker api/app scheduler/scheduler`
Expected: green.

```bash
git add api/app/schemas/release_notes.py api/app/queries/release_notes.py api/app/routes/v1/release_notes.py api/app/routes/v1/__init__.py api/README.md api/tests/test_v1_release_notes.py
```

---

### Task 5: Release name on created GitHub issues (R4, worker side)

**Files:**
- Modify: `worker/worker/ticket_issue_runner.py:194-218` (`_format_issue_body`), `worker/worker/ticket_issue_runner.py:470-498` (`_format_parent_body`)
- Test: `worker/tests/test_ticket_issue_runner.py` (append two tests after `test_parent_body_summary_prefers_english_plan_summary`)

**Interfaces:**
- Consumes: `TicketIssueJobParams.release: ReleaseRef | None` (Task 2).
- Produces: issue bodies carry `**Release:** <name>` directly after the `**Branch:**` line (child) or after the summary/branch block (parent) when `params.release.name` is non-blank.

- [ ] **Step 1: Write the failing tests**

Append to `worker/tests/test_ticket_issue_runner.py`:

```python
def _issue_params(**over) -> TicketIssueJobParams:
    return TicketIssueJobParams(**{**dict(
        run_id=1, odoo_instance_id=1, ticket_id=123, model_name="helpdesk.ticket",
        github_url="https://github.com/acme/widgets", name="Login",
        description="Wir brauchen eine Login-Seite.", analysis_html="",
        priority="1", ticket_url="https://odoo.example.com/web#id=123",
    ), **over})


def test_issue_body_names_the_release_after_the_branch():
    """R4: the release the ticket ships in is named right under the branch;
    no line at all when the request carries no release or a blank name."""
    from worker.ticket_issue_runner import _format_issue_body

    item = {"body": "Add the login form.", "acceptance_criteria": ["Form renders"]}
    release = {"id": 3275, "name": "Lollipop", "date": None}

    with_release = _format_issue_body(item, _issue_params(release=release), "revaticketX", "feat/123")
    assert "**Branch:** `feat/123`\n**Release:** Lollipop\n" in with_release

    without = _format_issue_body(item, _issue_params(), "revaticketX", "feat/123")
    assert "Release" not in without

    blank = _format_issue_body(
        item, _issue_params(release={"id": 3275, "name": "  ", "date": None}), "revaticketX", "feat/123"
    )
    assert "Release" not in blank


def test_parent_body_names_the_release():
    from worker.ticket_issue_runner import _format_parent_body

    release = {"id": 3275, "name": "Lollipop", "date": None}
    body = _format_parent_body(
        _issue_params(release=release), "revaticketX", "revaticketparentX", plan_summary="Login page"
    )
    assert "\n\n**Release:** Lollipop\n\n---\n" in body

    plain = _format_parent_body(_issue_params(), "revaticketX", "revaticketparentX", plan_summary="Login page")
    assert "Release" not in plain
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_runner.py -k "names_the_release" -q`
Expected: both fail on the missing `**Release:**` line.

- [ ] **Step 3: Add the line to both bodies**

In `_format_issue_body`, replace
```python
    lines += ["", f"**Branch:** `{branch}`"]
```
with
```python
    lines += ["", f"**Branch:** `{branch}`"]
    # R4: the release the ticket ships in (Odoo's optional `release` block).
    if params.release and params.release.name.strip():
        lines += [f"**Release:** {params.release.name.strip()}"]
```

In `_format_parent_body`, after the `if issues:` block that appends the branch line and before `lines += ["", "---", ...]`, add:
```python
    if params.release and params.release.name.strip():
        lines += ["", f"**Release:** {params.release.name.strip()}"]
```

- [ ] **Step 4: Run the tests**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_issue_runner.py -q`
Expected: all pass (existing body assertions are unaffected because they never set `release`).

- [ ] **Step 5: Full check and stage**

Run: `make test && ruff check reva worker/worker api/app scheduler/scheduler`

```bash
git add worker/worker/ticket_issue_runner.py worker/tests/test_ticket_issue_runner.py
```

---

### Task 6: Callback client, contracts, sync to Cloudunify

**Files:**
- Modify: `reva/odoo_contracts.py` (payload model after `ChangeSummaryPayload`; two `Contract` entries; `_inbound_models`; `create-issues` extra sample)
- Modify: `reva/odoo_client.py:1-40` (docstring endpoint list), `reva/odoo_client.py:48-58` (import), new method after `change_summary`
- Modify: `api/app/schemas/ticket_issues.py` (`CreateIssuesRequest.release`)
- Modify: `api/tests/test_contracts_inbound.py:16-24` (`_MODELS`)
- Regenerate: `contracts/` (all JSON under `contracts/inbound` and `contracts/callbacks`, `manifest.json`)
- Modify (other repo, not staged): `../Cloudunify/reva_contracts/` (rsync), `../Cloudunify/custom_addons/cu_reva_connector/tests/test_contracts.py:11` (pin) and its `test_callback_paths_are_namespaced`
- Test: `worker/tests/test_odoo_client.py` (append), `api/tests/test_v1_ticket_issues.py` is untouched; new schema test in `api/tests/test_contracts_inbound.py`

**Interfaces:**
- Consumes: `ReleaseNoteRequest` (Task 4), `ReleaseRef` (Task 2).
- Produces: `reva.odoo_contracts.ReleaseNotePayload(release_id: int, note_id: int, status: Literal["completed","failed"], url: str | None = None, html: str | None = None, css: str | None = None, error: str | None = None)`; `OdooCallbackClient.release_note(*, release_id: int, note_id: int, status: str, url: str | None = None, html: str | None = None, css: str | None = None, error: str | None = None) -> None` posting to `/releases/release-note`; contracts `releases.release-note` (reva->odoo) and `release-note` (odoo->reva); `CreateIssuesRequest.release: ReleaseRef | None = None`.

- [ ] **Step 1: Write the failing tests**

Append to `worker/tests/test_odoo_client.py`:

```python
def test_release_note_posts_completed_payload(monkeypatch):
    seen: dict = {}

    def post(url, **kwargs):
        seen["url"] = url
        seen["json"] = kwargs["json"]
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)

    _client().release_note(
        release_id=3275, note_id=7, status="completed",
        url="https://reva.example.com/docs/?repo=1&path=docs/releases/lollipop.html",
        html='<div class="rl-page"></div>', css=".rl-page{}",
    )

    assert seen["url"] == "https://odoo.example.com/api/reva/releases/release-note"
    assert seen["json"] == {
        "release_id": 3275, "note_id": 7, "status": "completed",
        "url": "https://reva.example.com/docs/?repo=1&path=docs/releases/lollipop.html",
        "html": '<div class="rl-page"></div>', "css": ".rl-page{}", "error": None,
    }


def test_release_note_failed_payload_and_409_is_permanent(monkeypatch):
    seen: dict = {}

    def post(url, **kwargs):
        seen["json"] = kwargs["json"]
        return httpx.Response(409, text='{"detail":"Stale note_id"}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)

    with pytest.raises(PermanentError):
        _client().release_note(
            release_id=3275, note_id=7, status="failed",
            error="Kein Release-Log 'docs/releases/lollipop.html' in acme/widgets",
        )
    assert seen["json"]["status"] == "failed"
    assert seen["json"]["url"] is None and seen["json"]["html"] is None and seen["json"]["css"] is None
```

Append to `api/tests/test_contracts_inbound.py`:

```python
def test_create_issues_release_block_is_typed():
    from reva.types import ReleaseRef

    body = CreateIssuesRequest.model_validate({
        "ticket_id": 42, "model_name": "project.task",
        "github_url": "https://github.com/acme/widgets", "name": "Login rework",
        "description": "Please add a login page.", "analysis_html": "", "priority": "1",
        "ticket_url": "https://odoo.example.com/web#id=42",
        "release": {"id": 3275, "name": "Lollipop", "date": "2026-09-30 00:00:00"},
    })
    assert body.release == ReleaseRef(id=3275, name="Lollipop", date="2026-09-30 00:00:00")
```

and add `"release-note": ReleaseNoteRequest,` to `_MODELS` with `from app.schemas.release_notes import ReleaseNoteRequest` in the imports.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_odoo_client.py -k release_note -q; cd ../api && .venv/bin/python -m pytest tests/test_contracts_inbound.py -q`
Expected: AttributeError (`release_note` missing); `test_all_inbound_request_contracts_covered` fails (model listed, no contract yet); the release-block test fails (field ignored).

- [ ] **Step 3: Payload model and contracts**

In `reva/odoo_contracts.py`, after `class ChangeSummaryPayload` add:

```python
class ReleaseNotePayload(BaseModel):
    """POST {base}/releases/release-note — the release-log lookup result
    (spec 2026-09-04-release-log-requirements, R2). `url`, `html` and `css` are
    set on `completed`; `error` on `failed`. Odoo composes html + css into one
    document and stores the URL as the "In REVA öffnen" link."""

    release_id: int
    note_id: int
    status: Literal["completed", "failed"]
    url: str | None = None
    html: str | None = None
    css: str | None = None
    error: str | None = None
```

In `_inbound_models()`, add `from app.schemas.release_notes import ReleaseNoteRequest` next to the other imports and `"release-note": ReleaseNoteRequest,` to the returned dict.

In `CONTRACTS`, after the `tickets.change-summary` entry add:

```python
    Contract(
        name="releases.release-note",
        direction="reva->odoo",
        method="POST",
        path="/releases/release-note",
        auth="bearer:instance-outbound-key",
        model=ReleaseNotePayload,
        sample={
            "release_id": 3275,
            "note_id": 12,
            "status": "completed",
            "url": "https://reva.example.com/docs/?repo=4&path=docs/releases/lollipop.html",
            "html": '<div class="rl-page"><header class="rl-masthead"><h1>Lollipop</h1></header></div>',
            "css": ".rl-page{--rl-ground:#fbfbf9}",
            "error": None,
        },
        extra_samples=[{
            "release_id": 3275,
            "note_id": 13,
            "status": "failed",
            "url": None,
            "html": None,
            "css": None,
            "error": "Kein Release-Log 'docs/releases/lollipop.html' in acme/widgets, acme/other",
        }],
    ),
```

After the `timesheet-review` inbound entry add:

```python
    Contract(
        name="release-note",
        direction="odoo->reva",
        method="POST",
        path="/api/v1/release-note",
        auth="bearer:instance-inbound-key",
        sample={
            "release_id": 3275,
            "name": "Lollipop",
            "date": "2026-09-30 00:00:00",
            "model_name": "project.task",
            "task_ids": [7595, 7620],
        },
    ),
```

In the `create-issues` entry add
```python
        extra_samples=[{
            "ticket_id": 42,
            "model_name": "project.task",
            "github_url": "https://github.com/acme/widgets",
            "name": "Login rework",
            "description": "Please add a login page.",
            "analysis_html": "",
            "description_docx": None,
            "priority": "1",
            "ticket_url": "https://odoo.example.com/web#id=42",
            "issue_type": None,
            "github_username": None,
            "github_project_url": None,
            "plan_date": None,
            "release": {"id": 3275, "name": "Lollipop", "date": "2026-09-30 00:00:00"},
        }],
```
after its `sample=`.

- [ ] **Step 4: Request schema field**

In `api/app/schemas/ticket_issues.py`, import `ReleaseRef` from `reva.types` (extend the existing `from reva.types import Attachment` line) and add to `CreateIssuesRequest` after `plan_date`:

```python
    release: ReleaseRef | None = Field(
        default=None,
        description="Optional Odoo release the record ships in ({id, name, date}); "
        "its name is written on every created issue.",
    )
```

- [ ] **Step 5: Client method**

In `reva/odoo_client.py`: add `ReleaseNotePayload,` to the `from reva.odoo_contracts import (...)` block (alphabetical), add the line
```
    POST {base}/releases/release-note  — release-log lookup result (url + fragment + theme css)
```
to the endpoint list in the module docstring, and after `change_summary` add:

```python
    def release_note(
        self,
        *,
        release_id: int,
        note_id: int,
        status: str,
        url: str | None = None,
        html: str | None = None,
        css: str | None = None,
        error: str | None = None,
    ) -> None:
        """Deliver a release-log lookup to the release (spec 2026-09-04, R2).
        Odoo's `ensure_pending` + note_id guard make retries safe: a stale or
        replayed delivery answers 409, which `_post` maps to PermanentError."""
        payload = ReleaseNotePayload(
            release_id=release_id,
            note_id=note_id,
            status=status,  # type: ignore[arg-type]
            url=url,
            html=html,
            css=css,
            error=error,
        )
        self._post("/releases/release-note", payload.model_dump())
        logger.bind(release_id=release_id, note_id=note_id, status=status).info(
            "odoo_release_note_ok"
        )
```

- [ ] **Step 6: Regenerate contracts and run the drift tests**

Run from the repo root:
```bash
worker/.venv/bin/python -m reva.odoo_contracts generate
git status --short contracts/
```
Expected: `manifest.json`, `contracts/inbound/release-note.*`, `contracts/callbacks/releases.release-note.*` (schema + sample + sample2), and `contracts/inbound/create-issues.schema.json` + `create-issues.sample2.json` change/appear.

Run: `cd worker && .venv/bin/python -m pytest tests/test_contracts_drift.py tests/test_odoo_contracts.py tests/test_contracts_generator.py tests/test_odoo_client.py -q; cd ../api && .venv/bin/python -m pytest tests/test_contracts_inbound.py tests/test_v1_ticket_issues.py -q`
Expected: all pass.

- [ ] **Step 7: Sync to Cloudunify and bump its pin (files stay uncommitted there)**

```bash
scripts/sync_contracts.sh ../Cloudunify
python3 -c "import json;print(json.load(open('contracts/manifest.json'))['contracts_version'])"
```
Put the printed hash into `../Cloudunify/custom_addons/cu_reva_connector/tests/test_contracts.py:11` (`CONTRACTS_VERSION = "..."`). In the same file's `test_callback_paths_are_namespaced`, add:
```python
        self.assertEqual(paths["releases.release-note"], "/releases/release-note")
        self.assertEqual(paths["release-note"], "/api/v1/release-note")
```
Run `cd ../Cloudunify && git status --short reva_contracts custom_addons/cu_reva_connector/tests` and list the changed files in your report. Do not stage or commit in `../Cloudunify`; Joseph commits there. This sync also lands the `reassign-issue` contract that has been owed since 2026-08-21.

- [ ] **Step 8: Full check and stage**

Run: `make test && ruff check reva worker/worker api/app scheduler/scheduler`

```bash
git add reva/odoo_contracts.py reva/odoo_client.py api/app/schemas/ticket_issues.py api/tests/test_contracts_inbound.py worker/tests/test_odoo_client.py contracts/
```

---

### Task 7: Worker: release-log lookup job

**Files:**
- Create: `worker/worker/release_note_tasks.py`
- Create: `worker/worker/release_note_runner.py`
- Test: `worker/tests/test_release_note_runner.py` (new)

**Interfaces:**
- Consumes: `writers.get_release_note/record_release_note_completed/record_release_note_failed/record_release_note_callback_sent/list_enabled_repositories/get_odoo_instance/record_ops_event` (Task 3 + existing), `reva.release_log` and `reva.config.DOCS_SITE_URL` (Task 2), `worker.repo_config.load_repo_config`, `worker.runner.build_odoo_client/get_context`, `OdooCallbackClient.release_note` (Task 6), `ctx.github.get_installation_token(installation_id)` and `ctx.github.get_file_content(token, owner, repo, path, ref) -> str | None`.
- Produces: RQ entry `worker.release_note_tasks.run_release_note(job_params: dict) -> dict` (wrapped in `terminal_on_permanent`).

- [ ] **Step 1: Write the failing tests**

Create `worker/tests/test_release_note_runner.py`:

```python
"""Tests for release_note_runner.run_release_note (spec 2026-09-04, R2)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy import select

from reva import config
from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import OdooInstance, OpsEvent, Repository
from reva.errors import PermanentError, TransientError
from reva.release_log import release_slug
from worker.release_note_runner import run_release_note
from worker.runner import WorkerContext, set_context

PAGE = '<div class="rl-page"><header class="rl-masthead"><h1>Lollipop</h1></header></div>\n'


@dataclass
class FakeGitHub:
    # (owner, repo, path, ref) -> content; a missing key is a 404 (None)
    files: dict[tuple[str, str, str, str], str] = field(default_factory=dict)
    raise_exc: Exception | None = None
    # repo names whose every read fails permanently (a 403, a deleted repo)
    fail_repos: set[str] = field(default_factory=set)
    reads: list[tuple[str, str, str]] = field(default_factory=list)

    def get_installation_token(self, installation_id: int) -> str:
        return f"tok-{installation_id}"

    def get_file_content(self, token, owner, repo, path, ref):
        if self.raise_exc is not None:
            raise self.raise_exc
        if repo in self.fail_repos:
            raise PermanentError(f"GitHub 403 for {owner}/{repo}")
        self.reads.append((owner, repo, path))
        return self.files.get((owner, repo, path, ref))


@dataclass
class FakeOdoo:
    raise_exc: Exception | None = None
    calls: list[dict] = field(default_factory=list)

    def release_note(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Database(engine)
    with db.session() as s:
        s.add(OdooInstance(
            id=1, name="wenatex", key_hash="h1", key_prefix="p1",
            callback_url="https://odoo.example.com/api/reva", callback_api_key_enc="",
        ))
    github = FakeGitHub()
    odoo = FakeOdoo()
    ctx = WorkerContext(
        db=db,
        claude=None,  # type: ignore[arg-type]
        runner=None,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        reviewer=None,  # type: ignore[arg-type]
        auditor=None,  # type: ignore[arg-type]
        ticket_analyzer=None,  # type: ignore[arg-type]
        verifier=None,  # type: ignore[arg-type]
    )
    monkeypatch.setattr("worker.release_note_runner.build_odoo_client", lambda ctx, _id: odoo)
    monkeypatch.setattr(config, "DOCS_SITE_URL", "https://reva.example.com")
    set_context(ctx)
    return {"db": db, "github": github, "odoo": odoo}


def _repo(db, rid, full_name, enabled=True):
    owner, name = full_name.split("/")
    with db.session() as s:
        s.add(Repository(
            id=rid, github_repository_id=1000 + rid, owner=owner, name=name,
            full_name=full_name, default_branch="main", installation_id=7, enabled=enabled,
        ))


def _map(github, full_name, instance="wenatex"):
    owner, name = full_name.split("/")
    github.files[(owner, name, ".claude-review.yml", "main")] = f"odoo_instance: {instance}\n"


def _page(github, full_name, slug="lollipop", content=PAGE):
    owner, name = full_name.split("/")
    github.files[(owner, name, f"docs/releases/{slug}.html", "main")] = content


def _params(db, name="Lollipop"):
    note_id = writers.record_release_note_created(
        db, odoo_instance_id=1, release_id=3275, release_name=name, slug=release_slug(name)
    )
    return {"note_id": note_id, "odoo_instance_id": 1, "release_id": 3275,
            "release_name": name, "slug": release_slug(name)}


def _ops_events(db):
    with db.session() as s:
        return [(e.component, e.severity, e.event) for e in s.execute(select(OpsEvent)).scalars()]


def test_hit_sends_url_fragment_and_css(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets")
    _page(gh, "acme/widgets")
    params = _params(db)

    out = run_release_note(params)

    assert out == {"status": "completed", "note_id": params["note_id"]}
    call = odoo.calls[0]
    assert (call["release_id"], call["note_id"], call["status"]) == (3275, params["note_id"], "completed")
    assert call["url"] == "https://reva.example.com/docs/?repo=1&path=docs/releases/lollipop.html"
    assert call["html"] == PAGE
    assert ".rl-page" in call["css"]
    assert call["error"] is None
    row = writers.get_release_note(db, params["note_id"])
    assert row["status"] == "completed"
    assert (row["source_repo_id"], row["source_path"]) == (1, "docs/releases/lollipop.html")
    assert row["url"] == call["url"]
    assert row["callback_sent_at"] is not None and row["completed_at"] is not None
    assert _ops_events(db) == []


def test_unmapped_and_disabled_repos_are_never_read(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _repo(db, 2, "acme/other")           # no odoo_instance key
    _repo(db, 3, "acme/off", enabled=False)
    _map(gh, "acme/widgets")
    _map(gh, "acme/off")
    for r in ("acme/widgets", "acme/other", "acme/off"):
        _page(gh, r)

    run_release_note(_params(db))

    assert ("acme", "other", "docs/releases/lollipop.html") not in gh.reads
    assert ("acme", "off", ".claude-review.yml") not in gh.reads
    assert odoo.calls[0]["url"].startswith("https://reva.example.com/docs/?repo=1&")


def test_other_instance_mapping_does_not_match(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets", instance="someone-else")
    _page(gh, "acme/widgets")
    params = _params(db)

    with pytest.raises(PermanentError):
        run_release_note(params)

    assert odoo.calls[0]["error"] == "Kein Repository mit `odoo_instance: wenatex` in .claude-review.yml"


def test_ambiguous_hit_takes_lowest_repo_id_and_records_event(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 2, "acme/second")
    _repo(db, 1, "acme/first")
    for r in ("acme/first", "acme/second"):
        _map(gh, r)
        _page(gh, r)

    run_release_note(_params(db))

    assert odoo.calls[0]["url"] == "https://reva.example.com/docs/?repo=1&path=docs/releases/lollipop.html"
    assert ("release_log", "info", "release_doc_ambiguous") in _ops_events(db)


def test_no_release_log_sends_german_failure(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _repo(db, 2, "acme/other")
    _map(gh, "acme/widgets")
    _map(gh, "acme/other")
    params = _params(db, name="Big Bang 2")

    with pytest.raises(PermanentError):
        run_release_note(params)

    call = odoo.calls[0]
    assert call["status"] == "failed"
    assert call["error"] == "Kein Release-Log 'docs/releases/big-bang-2.html' in acme/widgets, acme/other"
    assert call["url"] is None and call["html"] is None and call["css"] is None
    row = writers.get_release_note(db, params["note_id"])
    assert row["status"] == "failed" and row["error"] == call["error"]
    assert row["callback_sent_at"] is not None
    assert _ops_events(db) == []   # a missing page is an outcome, not a degradation


def test_no_mapped_repo_names_the_missing_key(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")   # no .claude-review.yml at all
    params = _params(db)

    with pytest.raises(PermanentError):
        run_release_note(params)

    assert odoo.calls[0]["error"] == "Kein Repository mit `odoo_instance: wenatex` in .claude-review.yml"
    assert writers.get_release_note(db, params["note_id"])["status"] == "failed"


def test_failed_callback_error_is_visible_but_does_not_mask_the_reason(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets")
    odoo.raise_exc = TransientError("Odoo 503")
    params = _params(db)

    with pytest.raises(PermanentError) as exc:
        run_release_note(params)

    assert "Kein Release-Log" in str(exc.value)
    row = writers.get_release_note(db, params["note_id"])
    assert row["status"] == "failed" and row["callback_sent_at"] is None
    assert ("odoo_callback", "error", "release_note_failed_callback_error") in _ops_events(db)


def test_transient_callback_keeps_row_pending_and_retries(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets")
    _page(gh, "acme/widgets")
    params = _params(db)
    odoo.raise_exc = TransientError("Odoo 503")

    with pytest.raises(TransientError):
        run_release_note(params)

    assert writers.get_release_note(db, params["note_id"])["status"] == "pending"
    assert ("odoo_callback", "error", "release_note_callback_failed") in _ops_events(db)

    odoo.raise_exc = None
    run_release_note(params)

    assert len(odoo.calls) == 2
    assert writers.get_release_note(db, params["note_id"])["status"] == "completed"


def test_permanent_callback_marks_failed_without_second_callback(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets")
    _page(gh, "acme/widgets")
    params = _params(db)
    odoo.raise_exc = PermanentError("Odoo /releases/release-note 409 (permanent): Stale note_id")

    with pytest.raises(PermanentError):
        run_release_note(params)

    row = writers.get_release_note(db, params["note_id"])
    assert row["status"] == "failed" and "409" in row["error"]
    assert len(odoo.calls) == 1
    assert ("odoo_callback", "error", "release_note_callback_failed") in _ops_events(db)


def test_resume_completed_does_not_call_odoo_again(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets")
    _page(gh, "acme/widgets")
    params = _params(db)
    run_release_note(params)

    out = run_release_note(params)

    assert out == {"status": "completed", "note_id": params["note_id"]}
    assert len(odoo.calls) == 1


def test_github_outage_is_transient_and_leaves_row_pending(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    gh.raise_exc = TransientError("GitHub 502")
    params = _params(db)

    with pytest.raises(TransientError):
        run_release_note(params)

    assert odoo.calls == []
    assert writers.get_release_note(db, params["note_id"])["status"] == "pending"


def test_unreadable_config_is_skipped_with_ops_event(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/broken")
    _repo(db, 2, "acme/widgets")
    _map(gh, "acme/widgets")
    _page(gh, "acme/widgets")
    gh.fail_repos.add("broken")

    run_release_note(_params(db))

    assert odoo.calls[0]["status"] == "completed"
    assert ("release_log", "warning", "config_fetch_failed") in _ops_events(db)


def test_unset_docs_site_url_is_relative_and_visible(env, monkeypatch):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    monkeypatch.setattr(config, "DOCS_SITE_URL", "")
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets")
    _page(gh, "acme/widgets")

    run_release_note(_params(db))

    assert odoo.calls[0]["url"] == "/docs/?repo=1&path=docs/releases/lollipop.html"
    assert ("release_log", "warning", "docs_site_url_unset") in _ops_events(db)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_release_note_runner.py -q`
Expected: ImportError (`worker.release_note_runner` missing).

- [ ] **Step 3: Stable task entry**

Create `worker/worker/release_note_tasks.py`:

```python
"""Stable RQ task entry point for the release-log lookup.

Import path used when enqueuing: "worker.release_note_tasks.run_release_note".
Enqueued with retry=, so it goes through the shared task contract: a
PermanentError ends the job terminally instead of RQ re-running it (and
re-firing the failed Odoo callback); TransientError still retries.
"""

from worker.release_note_runner import run_release_note as _run_release_note
from worker.task_contract import terminal_on_permanent

run_release_note = terminal_on_permanent(_run_release_note)

__all__ = ["run_release_note"]
```

- [ ] **Step 4: Runner**

Create `worker/worker/release_note_runner.py`:

```python
"""Release-log lookup job (spec docs/superpowers/specs/2026-09-04-release-log-requirements.md, R2).

No Claude call and no task material: find `docs/releases/<slug>.html` in the
repos mapped to the calling Odoo instance and hand Odoo the docs-site URL, the
fragment and the theme CSS, or a `failed` status with a German reason.

The row turns `completed` only after Odoo accepted the callback, so an RQ
retry (TransientError anywhere) repeats the cheap lookup; nothing is stored
to resend. `failed` is terminal: the reason is recorded, Odoo is told best
effort, and the job ends through the shared task contract.
"""

from __future__ import annotations

from typing import NoReturn

import structlog

from reva import config, release_log
from reva.db import writers
from reva.errors import PermanentError, TransientError
from reva.types import ReleaseNoteJobParams
from worker.repo_config import load_repo_config
from worker.runner import build_odoo_client, get_context

logger = structlog.get_logger()


def _mapped_repos(ctx, instance_name: str, log) -> list[dict]:
    """Enabled repos whose `.claude-review.yml` on the default branch declares
    `odoo_instance: <instance_name>`, ascending id, each with its installation
    token. One config fetch per enabled repo, the way reviews and audits read
    it. A repo whose config cannot be fetched is skipped with an ops event: a
    broken repo must not hide the release log sitting in another one. A GitHub
    outage propagates as TransientError so RQ retries the whole lookup."""
    mapped: list[dict] = []
    for repo in writers.list_enabled_repositories(ctx.db):
        try:
            token = ctx.github.get_installation_token(repo["installation_id"])
            cfg = load_repo_config(
                ctx.github, token, repo["owner"], repo["name"], repo["default_branch"]
            )
        except TransientError:
            raise
        except Exception as exc:  # noqa: BLE001 — degrade per repo, visibly
            log.warning("release_log_config_fetch_failed", repo=repo["full_name"], error=str(exc))
            writers.record_ops_event(
                ctx.db, "release_log", "warning", "config_fetch_failed",
                {"repo": repo["full_name"], "error": str(exc)[:300]},
            )
            continue
        if cfg.odoo_instance == instance_name:
            mapped.append({**repo, "token": token})
    return mapped


def _find_release_log(
    ctx, params: ReleaseNoteJobParams, mapped: list[dict], log
) -> tuple[dict, str] | None:
    """(repo, html) for the first mapped repo (ascending id) holding
    docs/releases/<slug>.html; None when none has it. Several hits are an ops
    event so the duplicate gets cleaned up on the repo side."""
    path = release_log.release_log_path(params.slug)
    hits: list[tuple[dict, str]] = []
    for repo in mapped:
        content = ctx.github.get_file_content(
            repo["token"], repo["owner"], repo["name"], path, repo["default_branch"]
        )
        if content:
            hits.append((repo, content))
    if not hits:
        return None
    if len(hits) > 1:
        repos = [r["full_name"] for r, _ in hits]
        log.warning("release_doc_ambiguous", repos=repos)
        writers.record_ops_event(
            ctx.db, "release_log", "info", "release_doc_ambiguous",
            {"note_id": params.note_id, "slug": params.slug, "repos": repos},
        )
    return hits[0]


def _fail(ctx, params: ReleaseNoteJobParams, error: str, log) -> NoReturn:
    """Record the failure, tell Odoo (best effort, never masks the reason) and
    end the job terminally."""
    log.warning("release_note_failed", error=error)
    writers.record_release_note_failed(ctx.db, params.note_id, error)
    try:
        odoo = build_odoo_client(ctx, params.odoo_instance_id)
        odoo.release_note(
            release_id=params.release_id, note_id=params.note_id, status="failed", error=error
        )
        writers.record_release_note_callback_sent(ctx.db, params.note_id)
    except Exception:  # noqa: BLE001
        log.warning("release_note_failed_callback_error", exc_info=True)
        writers.record_ops_event(
            ctx.db, "odoo_callback", "error", "release_note_failed_callback_error",
            {"note_id": params.note_id, "release_id": params.release_id},
        )
    raise PermanentError(error)


def run_release_note(job_params: dict) -> dict:
    """RQ task entry point for the release-log lookup."""
    ctx = get_context()
    params = ReleaseNoteJobParams.model_validate(job_params)
    log = logger.bind(
        note_id=params.note_id,
        release_id=params.release_id,
        odoo_instance_id=params.odoo_instance_id,
        slug=params.slug,
    )
    log.info("release_note_start")

    row = writers.get_release_note(ctx.db, params.note_id)
    if row is None:
        raise PermanentError(f"release note {params.note_id} not found")
    if row["status"] == "completed":
        log.info("release_note_resume_completed")
        return {"status": "completed", "note_id": params.note_id}
    if row["status"] == "failed":
        raise PermanentError(row["error"] or "release note already failed")

    instance = writers.get_odoo_instance(ctx.db, params.odoo_instance_id)
    if instance is None:
        raise PermanentError(f"odoo_instance {params.odoo_instance_id} not found")

    path = release_log.release_log_path(params.slug)
    try:
        mapped = _mapped_repos(ctx, instance["name"], log)
        hit = _find_release_log(ctx, params, mapped, log) if mapped else None
    except TransientError:
        log.warning("release_note_transient_error", exc_info=True)
        raise
    except PermanentError as exc:
        log.error("release_note_permanent_error", error=str(exc))
        _fail(ctx, params, f"GitHub-Zugriff fehlgeschlagen: {exc}", log)
    except Exception as exc:  # noqa: BLE001
        log.exception("release_note_unexpected_error")
        _fail(ctx, params, f"Unerwarteter Fehler: {exc}", log)

    if not mapped:
        _fail(
            ctx, params,
            f"Kein Repository mit `odoo_instance: {instance['name']}` in .claude-review.yml",
            log,
        )
    if hit is None:
        _fail(
            ctx, params,
            f"Kein Release-Log '{path}' in " + ", ".join(r["full_name"] for r in mapped),
            log,
        )
    repo, html = hit

    url = release_log.docs_site_page_url(repo["id"], path)
    if not config.DOCS_SITE_URL:
        log.warning("docs_site_url_unset", url=url)
        writers.record_ops_event(
            ctx.db, "release_log", "warning", "docs_site_url_unset",
            {"note_id": params.note_id, "url": url},
        )

    odoo = build_odoo_client(ctx, params.odoo_instance_id)
    try:
        odoo.release_note(
            release_id=params.release_id,
            note_id=params.note_id,
            status="completed",
            url=url,
            html=html,
            css=release_log.theme_css(),
        )
    except PermanentError as exc:
        # 401/404/409: Odoo will not take this delivery (stale note_id, release
        # no longer pending). Terminal; the reason stays readable in the TUI.
        log.warning("release_note_callback_permanent", exc_info=True)
        writers.record_release_note_failed(
            ctx.db, params.note_id, f"odoo callback rejected: {exc}"
        )
        writers.record_ops_event(
            ctx.db, "odoo_callback", "error", "release_note_callback_failed",
            {"note_id": params.note_id, "release_id": params.release_id},
        )
        raise
    except TransientError:
        log.warning("release_note_callback_error", exc_info=True)
        writers.record_ops_event(
            ctx.db, "odoo_callback", "error", "release_note_callback_failed",
            {"note_id": params.note_id, "release_id": params.release_id},
        )
        raise

    writers.record_release_note_completed(
        ctx.db, params.note_id, source_repo_id=repo["id"], source_path=path, url=url
    )
    log.info("release_note_done", repo=repo["full_name"])
    return {"status": "completed", "note_id": params.note_id}
```

- [ ] **Step 5: Run the tests**

Run: `cd worker && .venv/bin/python -m pytest tests/test_release_note_runner.py -q`
Expected: all pass.

- [ ] **Step 6: Type check and full check, then stage**

Run: `mypy reva worker/worker --ignore-missing-imports` (advisory: no new errors in the two new files) and `make test && ruff check reva worker/worker api/app scheduler/scheduler`.

```bash
git add worker/worker/release_note_tasks.py worker/worker/release_note_runner.py worker/tests/test_release_note_runner.py
```

---

### Task 8: TUI Releases tab (key `w`)

**Files:**
- Modify: `tui/internal/api/types.go` (after `TimesheetReviewPage`)
- Modify: `tui/internal/api/iface.go:20` (after `TimesheetReviews`)
- Modify: `tui/internal/api/client.go:160-163` (after `TimesheetReviews`)
- Modify: `tui/internal/api/mock.go:640-666` (after `TimesheetReviews`)
- Modify: `tui/internal/ui/messages.go:67-70` (after `timesheetsLoadedMsg`)
- Create: `tui/internal/ui/releases.go`
- Modify: `tui/internal/ui/app.go` (enum, `tabKeys`, struct, constructor, `Init`, resize, key routing, tick fan-out, loaded-msg case, `View`, `tabBar`, `statusBar`)
- Create: `tui/internal/ui/releases_test.go`
- Modify: `tui/internal/ui/overflow_test.go:57-103` (`loadedApp` + `views`)
- Modify: `README.md:211-213` (tab table row + global keys sentence)

**Interfaces:**
- Consumes: `GET /api/v1/release-notes?limit=N` → `ReleaseNotePage` (Task 4 field names).
- Produces: `api.ReleaseNoteSummary`, `api.ReleaseNotePage`, `ClientIface.ReleaseNotes(limit int) (*ReleaseNotePage, error)`, `releasesLoadedMsg{data *api.ReleaseNotePage; err error}`, `viewReleases`, tab key `w`.

- [ ] **Step 1: Write the failing test**

Create `tui/internal/ui/releases_test.go`:

```go
package ui

import (
	"errors"
	"strings"
	"testing"

	"reva-tui/internal/api"
)

func TestReleasesLoadAndView(t *testing.T) {
	r := newReleases(&api.MockClient{})
	r.width, r.height = 120, 30
	data, _ := (&api.MockClient{}).ReleaseNotes(100)
	r, _ = r.update(releasesLoadedMsg{data: data})

	if len(r.items) != 3 {
		t.Fatalf("expected 3 release rows, got %d", len(r.items))
	}
	out := r.view(120, 30)
	for _, want := range []string{"Release Logs", "Lollipop", "docs/releases/lollipop.html", "Marshmallow", "failed"} {
		if !strings.Contains(out, want) {
			t.Fatalf("view missing %q:\n%s", want, out)
		}
	}
}

func TestReleasesErrorView(t *testing.T) {
	r := newReleases(&api.MockClient{})
	r, _ = r.update(releasesLoadedMsg{err: errors.New("boom")})

	out := r.view(100, 20)
	if !strings.Contains(out, "Error: boom") {
		t.Fatalf("error view missing message:\n%s", out)
	}
}

func TestReleasesTabKey(t *testing.T) {
	if tabKeys["w"] != viewReleases {
		t.Fatalf("expected w to open the Releases tab")
	}
}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd tui && go test ./internal/ui/ -run 'TestReleases' 2>&1 | head`
Expected: compile error (`newReleases`, `releasesLoadedMsg`, `viewReleases` undefined).

- [ ] **Step 3: API types, interface, client, mock**

`tui/internal/api/types.go`, after `TimesheetReviewPage`:

```go
// ReleaseNoteSummary is one Odoo release-log lookup (GET /release-notes):
// which repo page REVA handed to Odoo for a release, or why it could not.
type ReleaseNoteSummary struct {
	ID             int        `json:"id"`
	OdooInstanceID int        `json:"odoo_instance_id"`
	ReleaseID      int        `json:"release_id"`
	ReleaseName    string     `json:"release_name"`
	Slug           string     `json:"slug"`
	Status         string     `json:"status"`
	SourceRepoID   *int       `json:"source_repo_id"`
	SourcePath     *string    `json:"source_path"`
	URL            *string    `json:"url"`
	Error          *string    `json:"error"`
	CallbackSentAt *time.Time `json:"callback_sent_at"`
	CreatedAt      time.Time  `json:"created_at"`
	CompletedAt    *time.Time `json:"completed_at"`
}

type ReleaseNotePage struct {
	Items []ReleaseNoteSummary `json:"items"`
	Total int                  `json:"total"`
}
```

`tui/internal/api/iface.go`, after `TimesheetReviews(limit int) (*TimesheetReviewPage, error)`:
```go
	ReleaseNotes(limit int) (*ReleaseNotePage, error)
```

`tui/internal/api/client.go`, after `TimesheetReviews`:
```go
func (c *Client) ReleaseNotes(limit int) (*ReleaseNotePage, error) {
	var p ReleaseNotePage
	return &p, c.get(fmt.Sprintf("/release-notes?limit=%d", limit), &p)
}
```

`tui/internal/api/mock.go`, after `TimesheetReviews`:
```go
func (m *MockClient) ReleaseNotes(limit int) (*ReleaseNotePage, error) {
	now := time.Now()
	strPtr := func(s string) *string { return &s }
	intPtr := func(i int) *int { return &i }
	done := now.Add(-50 * time.Second)
	failedAt := now.Add(-19 * time.Minute)
	items := []ReleaseNoteSummary{
		{
			ID: 3, OdooInstanceID: 1, ReleaseID: 3275, ReleaseName: "Lollipop", Slug: "lollipop",
			Status: "completed", SourceRepoID: intPtr(2),
			SourcePath: strPtr("docs/releases/lollipop.html"),
			URL:        strPtr("https://reva.example.com/docs/?repo=2&path=docs/releases/lollipop.html"),
			CallbackSentAt: &done, CreatedAt: now.Add(-1 * time.Minute), CompletedAt: &done,
		},
		{
			ID: 2, OdooInstanceID: 1, ReleaseID: 3277, ReleaseName: "Marshmallow", Slug: "marshmallow",
			Status: "failed",
			Error:  strPtr("Kein Release-Log 'docs/releases/marshmallow.html' in acme/widgets"),
			CreatedAt: now.Add(-20 * time.Minute), CompletedAt: &failedAt,
		},
		{
			ID: 1, OdooInstanceID: 2, ReleaseID: 12, ReleaseName: "Nougat", Slug: "nougat",
			Status: "pending", CreatedAt: now.Add(-30 * time.Second),
		},
	}
	n := min(limit, len(items))
	return &ReleaseNotePage{Items: items[:n], Total: len(items)}, nil
}
```

`tui/internal/ui/messages.go`, after `timesheetsLoadedMsg`:
```go
type releasesLoadedMsg struct {
	data *api.ReleaseNotePage
	err  error
}
```

- [ ] **Step 4: The tab**

Create `tui/internal/ui/releases.go`:

```go
package ui

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"reva-tui/internal/api"
)

// Releases lists Odoo release-log lookups (GET /release-notes): which repo
// page REVA handed to Odoo for a release, or why it could not. Read-only.
type Releases struct {
	client  api.ClientIface
	items   []api.ReleaseNoteSummary
	total   int
	err     error
	loading bool
	cursor  int
	offset  int
	width   int
	height  int
}

func newReleases(client api.ClientIface) Releases {
	return Releases{client: client, loading: true}
}

func (r Releases) load() tea.Cmd {
	return func() tea.Msg {
		data, err := r.client.ReleaseNotes(100)
		return releasesLoadedMsg{data: data, err: err}
	}
}

func (r Releases) update(msg tea.Msg) (Releases, tea.Cmd) {
	switch m := msg.(type) {
	case tickMsg:
		return r, r.load()
	case releasesLoadedMsg:
		r.loading = false
		r.err = m.err
		if m.data != nil {
			r.items = m.data.Items
			r.total = m.data.Total
		}
		if r.cursor >= len(r.items) {
			r.cursor, r.offset = 0, 0
		}
	case tea.KeyMsg:
		visibleRows := r.height - 12
		if visibleRows < 1 {
			visibleRows = 1
		}
		if c, o, ok := listNav(m.String(), r.cursor, r.offset, len(r.items), visibleRows); ok {
			r.cursor, r.offset = c, o
			return r, nil
		}
		switch m.String() {
		case "r", "R":
			r.loading = true
			return r, r.load()
		}
	}
	return r, nil
}

func (r Releases) view(w, h int) string {
	header := styleTitle.Padding(0, 1).Render(fmt.Sprintf("Release Logs  (%d)", r.total))
	if r.loading && len(r.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center, styleSubtitle.Render("Loading...")))
	}
	if r.err != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			styleStatusFailed.Render("  Error: "+r.err.Error()))
	}
	if len(r.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No release-log lookups yet")))
	}

	colID, colInst, colStatus, colSource, colAge := 6, 5, 10, 30, 9
	colRelease := w - colID - colInst - colStatus - colSource - colAge - 18
	if colRelease < 12 {
		colRelease = 12
	}
	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("  %-*s  %-*s  %-*s  %-*s  %-*s  %-*s",
			colID, "Note",
			colInst, "Inst",
			colStatus, "Status",
			colRelease, "Release",
			colSource, "Source",
			colAge, "Age"),
	)

	visibleRows := h - 12
	if visibleRows < 1 {
		visibleRows = 1
	}
	off := ensureVisible(r.offset, r.cursor, visibleRows, len(r.items))
	end := off + visibleRows
	if end > len(r.items) {
		end = len(r.items)
	}

	rows := []string{hdr}
	for i := off; i < end; i++ {
		item := r.items[i]
		source := ""
		if item.SourcePath != nil {
			source = *item.SourcePath
		}
		line := fmt.Sprintf("  %-*s  %-*d  %-*s  %-*s  %-*s  %-*s",
			colID, fmt.Sprintf("#%d", item.ID),
			colInst, item.OdooInstanceID,
			colStatus, item.Status,
			colRelease, truncate(item.ReleaseName, colRelease),
			colSource, truncate(source, colSource),
			colAge, relativeTime(item.CreatedAt),
		)
		if i == r.cursor {
			rows = append(rows, styleSelected.Width(w-2).Render(line))
			continue
		}
		rows = append(rows, fmt.Sprintf("  %-*s  %-*d  %s  %-*s  %-*s  %-*s",
			colID, fmt.Sprintf("#%d", item.ID),
			colInst, item.OdooInstanceID,
			padCell(releaseStatusStyle(item.Status).Render(item.Status), colStatus),
			colRelease, truncate(item.ReleaseName, colRelease),
			colSource, truncate(source, colSource),
			colAge, relativeTime(item.CreatedAt),
		))
	}

	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d", r.cursor+1, len(r.items)))
	detail := ""
	if r.cursor < len(r.items) {
		detail = r.detail(r.items[r.cursor], w)
	}
	return lipgloss.JoinVertical(lipgloss.Left, header, "", strings.Join(rows, "\n"), "", detail, "", pos)
}

func (r Releases) detail(item api.ReleaseNoteSummary, w int) string {
	var b strings.Builder
	b.WriteString(styleTitle.Render(fmt.Sprintf("#%d  %s  (%s)", item.ID, item.ReleaseName, item.Slug)) + "\n")
	b.WriteString(fmt.Sprintf("  Status    %s\n", releaseStatusStyle(item.Status).Render(item.Status)))
	b.WriteString(fmt.Sprintf("  Odoo      instance %d · release %d\n", item.OdooInstanceID, item.ReleaseID))
	if item.SourcePath != nil {
		repo := ""
		if item.SourceRepoID != nil {
			repo = fmt.Sprintf(" (repo %d)", *item.SourceRepoID)
		}
		b.WriteString(fmt.Sprintf("  Source    %s%s\n", *item.SourcePath, repo))
	}
	if item.URL != nil {
		b.WriteString(truncate(fmt.Sprintf("  URL       %s", *item.URL), w-4) + "\n")
	}
	b.WriteString(fmt.Sprintf("  Created   %s\n", relativeTime(item.CreatedAt)))
	if item.CompletedAt != nil {
		b.WriteString(fmt.Sprintf("  Done      %s\n", relativeTime(*item.CompletedAt)))
	}
	if item.CallbackSentAt != nil {
		b.WriteString(fmt.Sprintf("  Callback  %s\n", relativeTime(*item.CallbackSentAt)))
	}
	if item.Error != nil && *item.Error != "" {
		b.WriteString(styleStatusFailed.Render(truncate("  Error     "+*item.Error, w-4)) + "\n")
	}
	return styleBorder.Width(w - 2).Height(9).Render(b.String())
}

func releaseStatusStyle(status string) lipgloss.Style {
	switch status {
	case "completed":
		return styleStatusCompleted
	case "failed":
		return styleStatusFailed
	case "pending":
		return styleStatusStale
	default:
		return styleStatusOther
	}
}
```

- [ ] **Step 5: Wire the tab into `app.go`**

Apply, in order:
1. Enum: after `viewPersonas` add `viewReleases`.
2. `tabKeys`: add `"w": viewReleases,` after `"p": viewPersonas,`.
3. `App` struct: add `releases  Releases` after `personas  Personas`.
4. `NewApp`: add `releases:  newReleases(client),` after `personas:  newPersonas(client),`.
5. `Init`: add `a.releases.load(),` after `a.personas.load(),`.
6. `tea.WindowSizeMsg`: add `a.releases.width = m.Width` and `a.releases.height = contentH` after the personas pair.
7. Per-tab key routing: after the `if a.active == viewPersonas {...}` block add
```go
		if a.active == viewReleases {
			var cmd tea.Cmd
			a.releases, cmd = a.releases.update(msg)
			return a, cmd
		}
```
8. `tickMsg`: add `var releasesCmd tea.Cmd` / `a.releases, releasesCmd = a.releases.update(msg)` after the personas pair and `releasesCmd` to the `tea.Batch(...)` list before `tick()`.
9. Loaded messages: after the `supportThreadsLoadedMsg` case add
```go
	case releasesLoadedMsg:
		a.releases, _ = a.releases.update(msg)
```
10. `View`: add `case viewReleases: content = a.releases.view(a.width, contentH)` after the personas case.
11. `tabBar`: add `{"w", "Releases", 0, viewReleases},` after the Personas entry.
12. `statusBar`: add `case viewReleases: hint = "j/k navigate | r=refresh | q quit"` after the personas case.

The tab has no `/` filter and no status message, so `capturingText()` and `clearStatusMsgs()` stay untouched.

- [ ] **Step 6: Extend the overflow test**

In `tui/internal/ui/overflow_test.go`: in `loadedApp` add, after the personas lines,
```go
	rel, _ := mc.ReleaseNotes(100)
	a = apply(t, a, releasesLoadedMsg{data: rel})
```
and add `viewReleases` to the `views := []view{...}` slice. If the file keeps a `viewName` map, add `viewReleases: "Releases"` there too.

- [ ] **Step 7: Build, vet, test**

Run: `cd tui && go build ./... && go vet ./... && go test ./...`
Expected: green, including `TestReleasesLoadAndView`, `TestReleasesErrorView`, `TestReleasesTabKey` and the overflow tests. Then `go run . --demo`, press `w`: the three mock rows and the detail pane render.

- [ ] **Step 8: README**

In `README.md`, after the `| Timesheets | \`-\` | ...` row add:
```markdown
| Releases | `w` | Odoo release-log lookups from `GET /api/v1/release-notes`: which repo's `docs/releases/<slug>.html` REVA handed to Odoo (source path, docs-site URL) or the German reason it could not |
```
and in the "Global keys" line change `` `1–9` switch tabs `` to `` `1–9`, `0`, `-`, `=`, `p`, `w` switch tabs ``.

- [ ] **Step 9: Stage**

```bash
git add tui/internal/api/types.go tui/internal/api/iface.go tui/internal/api/client.go tui/internal/api/mock.go tui/internal/ui/messages.go tui/internal/ui/releases.go tui/internal/ui/releases_test.go tui/internal/ui/app.go tui/internal/ui/overflow_test.go README.md
```

---

### Task 9: Deployment wiring, docs, handoff, archive

**Files:**
- Modify: `docker-compose.yml` (worker `environment`), `docker-compose.prod.yml` (worker `environment`), `.env.example`
- Modify: `README.md` (environment variables table), `docs/setup-production.md` (env table)
- Modify: `HANDOFF.md` (new addendum at the top)
- Move: `docs/superpowers/specs/2026-09-04-release-log-requirements.md` → `docs/superpowers/specs/archive/`, `docs/superpowers/plans/2026-09-04-release-log.md` → `docs/superpowers/plans/archive/`

**Interfaces:**
- Consumes: everything above; nothing new produced.

- [ ] **Step 1: Environment wiring**

`docker-compose.yml`, worker `environment` block, after `REVA_CORE_KNOWLEDGE_DIR: /core`:
```yaml
      # Public base of the docs site, for the release-log link sent to Odoo.
      REVA_DOCS_SITE_URL: ${REVA_DOCS_SITE_URL:-}
```
`docker-compose.prod.yml`, worker `environment` block, after `REVA_DAILY_BUDGET_USD: ${REVA_DAILY_BUDGET_USD:-}`:
```yaml
      # Public base of the docs site (https://$REVA_DOMAIN), for the release-log
      # link sent to Odoo. Unset = relative link + ops event on every lookup.
      REVA_DOCS_SITE_URL: ${REVA_DOCS_SITE_URL:-}
```
`.env.example`, after the `REVA_TICKET_CODE_GROUNDING` line:
```bash

# --- Release-log lookup (worker) ----------------------------------------------
# Public base of the docs site; the release-log callback links Odoo to
# $REVA_DOCS_SITE_URL/docs/?repo=<id>&path=docs/releases/<slug>.html.
# REVA_DOCS_SITE_URL=https://reva.dev.cloudunify.org
```

- [ ] **Step 2: Env tables**

`README.md` environment table, after the `REVA_SECRET_KEY` row:
```markdown
| `REVA_DOCS_SITE_URL` | — | _(unset)_ | Public base of the docs site (e.g. `https://reva.dev.cloudunify.org`); the release-log callback links Odoo to `/docs/?repo=<id>&path=docs/releases/<slug>.html` under it. Unset: relative link plus a `docs_site_url_unset` ops event per lookup |
```
`docs/setup-production.md` env table, after the `REVA_CODEGRAPH_INDEX_TIMEOUT` row:
```markdown
| `REVA_DOCS_SITE_URL` | no | — | Public base of the docs site, normally `https://$REVA_DOMAIN`; the worker builds the release-log link Odoo shows from it. Set it, or every lookup records a `docs_site_url_unset` ops event |
```

- [ ] **Step 3: Handoff addendum**

Insert at the top of `HANDOFF.md`, directly under `# REVA — Work Handoff`:

```markdown
## Addendum 2026-09-04 — release-log lookup for Odoo

**Status: implemented, not deployed** (spec
`docs/superpowers/specs/archive/2026-09-04-release-log-requirements.md`, plan
`docs/superpowers/plans/archive/2026-09-04-release-log.md`). REVA no longer
drafts release notes: `POST /api/v1/release-note` (instance key, 202 with
`note_id`) enqueues `worker.release_note_tasks.run_release_note`, which reads
`.claude-review.yml` from every enabled repo, keeps those declaring
`odoo_instance: <instance name>`, fetches `docs/releases/<slug>.html` from
their default branch and posts `/releases/release-note` to Odoo with the
docs-site URL, the fragment and `reva/static/release-log.css`. No Claude call.
Migration `048_release_notes.sql` (SQL only unit-tested through the ORM model;
first Postgres run is the staging boot). TUI: **Releases** tab, key `w`.
Created GitHub issues now carry `**Release:** <name>` when Odoo sends the
`release` block (create-issues only).

**Deploy:** migration 048 at boot; worker + api images rebuilt (new `reva/`
package data and modules); nginx rebuilt for the SPA theme
(`docker compose -f docker-compose.prod.yml build nginx && … up -d nginx`);
set `REVA_DOCS_SITE_URL=https://$REVA_DOMAIN` in `.env` or every lookup logs
`docs_site_url_unset`. Each customer repo needs `odoo_instance: <name>` in its
`.claude-review.yml` (the name as registered in REVA's Odoo instances).

**Owed:** visual check of a real `docs/releases/lollipop.html` in the docs
site (cards, pills, stats, dark theme, print); end-to-end run against the
Cloudunify test instance (release 3275 "lollipop" once wenatex_odoo commits
its release page). Contracts synced to `../Cloudunify` (pin bumped in
`cu_reva_connector/tests/test_contracts.py`, uncommitted there; carries the
`reassign-issue` contract owed since 2026-08-21). Odoo side (R3 in the spec,
separate ticket): `url`/`css` on `ReleaseNoteRequest`, `release_note`
`sanitize=False`, composed document + iframe widget, drop the no-tasks guard
and the "REVA is drafting" wording.
```

- [ ] **Step 4: Archive spec and plan**

```bash
git mv docs/superpowers/specs/2026-09-04-release-log-requirements.md docs/superpowers/specs/archive/ 2>/dev/null || mv docs/superpowers/specs/2026-09-04-release-log-requirements.md docs/superpowers/specs/archive/
git mv docs/superpowers/plans/2026-09-04-release-log.md docs/superpowers/plans/archive/ 2>/dev/null || mv docs/superpowers/plans/2026-09-04-release-log.md docs/superpowers/plans/archive/
```
Then fix the two paths at the top of the moved spec (`Plan:` line) so they point at the archive locations.

- [ ] **Step 5: Final full check and stage**

Run: `make test && ruff check reva worker/worker api/app scheduler/scheduler && (cd tui && go build ./... && go vet ./... && go test ./...) && worker/.venv/bin/python -m pytest worker/tests/test_contracts_drift.py -q`
Expected: all green.

```bash
git add docker-compose.yml docker-compose.prod.yml .env.example README.md docs/setup-production.md HANDOFF.md docs/superpowers/specs/archive/2026-09-04-release-log-requirements.md docs/superpowers/plans/archive/2026-09-04-release-log.md
git status --short
```
Report the staged list and the unstaged `../Cloudunify` files. No commit.
