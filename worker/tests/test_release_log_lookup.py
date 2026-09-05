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
