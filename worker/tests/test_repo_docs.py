"""Repo-docs retrieval: scope, markdown sectionizer, and search."""

from __future__ import annotations

import pytest

from reva.db import Base, Database, create_engine_from_url
from reva.db.models import OpsEvent, RepoDocSection, RepoDocsSync
from reva.repo_docs import (
    _MAX_FILES,
    _SCOPE_VERSION,
    doc_priority,
    in_scope,
    search_repo_docs,
    split_markdown_sections,
    sync_repo_docs,
)


@pytest.fixture()
def db():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


class _FakeGitHub:
    """Records calls; serves a tree + per-path file contents."""

    def __init__(self, *, tree=None, files=None, default_branch="main",
                 install_error=None, file_errors=None):
        self.tree = tree if tree is not None else {"sha": "t1", "tree": [], "truncated": False}
        self.files = files or {}
        self.default_branch = default_branch
        self.install_error = install_error
        self.file_errors = file_errors or set()
        self.file_fetches = []
        self.tree_refs = []

    def get_repo_installation_id(self, owner, repo):
        if self.install_error:
            raise self.install_error
        return 99

    def get_installation_token(self, installation_id):
        return "ghs_tok"

    def get_repo(self, token, owner, repo):
        return {"default_branch": self.default_branch}

    def get_tree(self, token, owner, repo, ref, recursive=True):
        self.tree_refs.append(ref)
        return self.tree

    def get_file_content(self, token, owner, repo, path, ref):
        self.file_fetches.append((path, ref))
        if path in self.file_errors:
            raise RuntimeError(f"boom {path}")
        return self.files.get(path)


def _tree(sha, paths, truncated=False):
    return {
        "sha": sha,
        "truncated": truncated,
        "tree": [{"path": p, "type": "blob", "size": 10} for p in paths],
    }


def _ops(db, event=None):
    with db.session() as s:
        rows = s.query(OpsEvent).all()
        return [r for r in rows if event is None or r.event == event]


# ---- in_scope ---------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("custom_addons/cu_sale/README.md", True),
        ("custom-addons/cu_sale/README.md", True),          # hyphen variant
        ("custom_addons/cu_sale/docs/guide.markdown", True),
        ("custom_addons/cu_sale/README.MD", True),           # case-insensitive ext
        ("docs/setup-local.md", True),                       # repo-root docs folder
        ("docs/nested/deep/guide.md", True),
        ("docs/superpowers/specs/x-design.md", False),       # agent bookkeeping
        ("docs/superpowers/plans/y.md", False),
        ("docs/SUPERPOWERS/plans/y.md", False),               # case-insensitive segment
        ("custom_addons/cu_sale/docs/superpowers/z.md", False),  # segment anywhere
        ("docs/superpowers.md", True),                       # a FILE, not the folder
        ("custom_addons/cu_sale/CLAUDE.md", False),          # excluded basename
        ("custom_addons/CLAUDE.md", False),
        ("custom_addons/cu_sale/model.py", False),           # not markdown
        ("README.md", False),                                # loose root markdown
        ("CHANGELOG.md", False),
        ("docs/notes.txt", False),                           # not markdown
        ("documentation/guide.md", False),                   # prefix is anchored
        ("custom_addons/cu_sale/notes.txt", False),
    ],
)
def test_in_scope(path, expected):
    assert in_scope(path) is expected


# ---- split_markdown_sections ------------------------------------------------


def test_sectionizer_splits_on_atx_headings():
    text = "# Title One\nbody one\n\n## Title Two\nbody two\n"
    secs = split_markdown_sections("custom_addons/a/README.md", text)
    assert [s.title for s in secs] == ["Title One", "Title Two"]
    assert secs[0].body == "body one"
    assert secs[1].body == "body two"
    assert secs[0].anchor == "title-one"


def test_sectionizer_all_heading_levels():
    text = "".join(f"{'#' * n} H{n}\nb{n}\n" for n in range(1, 7))
    secs = split_markdown_sections("custom_addons/a/README.md", text)
    assert [s.title for s in secs] == ["H1", "H2", "H3", "H4", "H5", "H6"]


def test_sectionizer_preamble_titled_with_file_stem():
    text = "Intro paragraph before any heading.\n\n# Real Heading\ncontent\n"
    secs = split_markdown_sections("custom_addons/cu_sale/OVERVIEW.md", text)
    assert secs[0].title == "OVERVIEW"
    assert secs[0].body == "Intro paragraph before any heading."
    assert secs[1].title == "Real Heading"


def test_sectionizer_ignores_hashes_inside_code_fences():
    text = (
        "# Real\n"
        "```python\n"
        "# not a heading\n"
        "x = 1\n"
        "```\n"
        "~~~\n"
        "## also not a heading\n"
        "~~~\n"
        "still real body\n"
    )
    secs = split_markdown_sections("custom_addons/a/README.md", text)
    assert len(secs) == 1
    assert secs[0].title == "Real"
    assert "# not a heading" in secs[0].body
    assert "## also not a heading" in secs[0].body


def test_sectionizer_caps_body_length():
    text = "# Big\n" + ("x" * 5000)
    secs = split_markdown_sections("custom_addons/a/README.md", text)
    assert len(secs[0].body) == 2000


def test_sectionizer_empty_file():
    assert split_markdown_sections("custom_addons/a/README.md", "") == []
    assert split_markdown_sections("custom_addons/a/README.md", "\n\n  \n") == []


def test_sectionizer_atx_trailing_hashes_stripped():
    secs = split_markdown_sections("custom_addons/a/README.md", "## Title ##\nbody\n")
    assert secs[0].title == "Title"


# ---- search_repo_docs -------------------------------------------------------


def _seed(db, repo, rows):
    with db.session() as s:
        for path, title, body in rows:
            s.add(RepoDocSection(repo_full_name=repo, path=path, anchor="a", title=title, body=body))


def test_search_matches_terms_scoped_to_repo(db):
    _seed(db, "acme/widgets", [
        ("custom_addons/cu_sale/README.md", "Quotation templates", "custom quotation layout"),
        ("custom_addons/cu_hr/README.md", "Payroll", "salary rules"),
    ])
    _seed(db, "other/repo", [
        ("custom_addons/cu_sale/README.md", "Quotation templates", "different repo"),
    ])
    hits = search_repo_docs(db, "acme/widgets", ["quotation"])
    assert len(hits) == 1
    assert hits[0]["title"] == "Quotation templates"
    assert hits[0]["body"] == "custom quotation layout"  # not the other repo's row


def test_search_matches_any_term(db):
    # OR-of-terms: a section matching ANY planner term is a hit — the planner
    # sends up to 13 terms+modules, so demanding all of them would never match.
    _seed(db, "acme/widgets", [
        ("p1", "Quotation templates", "custom layout"),
        ("p2", "Payroll rules", "salary computation"),
        ("p3", "Unrelated", "nothing relevant"),
    ])
    hits = search_repo_docs(db, "acme/widgets", ["quotation", "payroll", "inventory"])
    assert {h["path"] for h in hits} == {"p1", "p2"}


def test_search_empty_terms_returns_empty(db):
    _seed(db, "acme/widgets", [("p1", "Quotation", "body")])
    assert search_repo_docs(db, "acme/widgets", []) == []
    assert search_repo_docs(db, "acme/widgets", ["  ", ""]) == []


# ---- sync_repo_docs ---------------------------------------------------------


def _sections(db, repo):
    with db.session() as s:
        return s.query(RepoDocSection).filter_by(repo_full_name=repo).all()


def test_sync_indexes_then_fresh_fast_path(db):
    gh = _FakeGitHub(
        tree=_tree("sha1", ["custom_addons/cu_sale/README.md"]),
        files={"custom_addons/cu_sale/README.md": "# Quotes\ncustom layout\n"},
    )
    r1 = sync_repo_docs(db, gh, "acme", "widgets")
    assert r1["status"] == "synced" and r1["sections"] == 1
    assert len(_sections(db, "acme/widgets")) == 1
    fetches_after_first = len(gh.file_fetches)

    # Same tree sha → fresh, zero additional file fetches.
    r2 = sync_repo_docs(db, gh, "acme", "widgets")
    assert r2["status"] == "fresh"
    assert len(gh.file_fetches) == fetches_after_first


def test_sync_reindexes_on_tree_sha_change(db):
    gh = _FakeGitHub(
        tree=_tree("sha1", ["custom_addons/a/README.md"]),
        files={"custom_addons/a/README.md": "# Old\nold body\n"},
    )
    sync_repo_docs(db, gh, "acme", "widgets")
    gh.tree = _tree("sha2", ["custom_addons/a/README.md"])
    gh.files = {"custom_addons/a/README.md": "# New\nnew body\n"}
    r = sync_repo_docs(db, gh, "acme", "widgets")
    assert r["status"] == "synced"
    secs = _sections(db, "acme/widgets")
    assert len(secs) == 1 and secs[0].title == "New"  # old rows deleted


def test_sync_reindexes_when_scope_version_is_stale(db):
    """A scope widening does not move the tree SHA, so the version stamp is the
    only thing that can force the re-index."""
    gh = _FakeGitHub(
        tree=_tree("sha1", ["custom_addons/a/README.md", "docs/guide.md"]),
        files={
            "custom_addons/a/README.md": "# H\nb\n",
            "docs/guide.md": "# G\nb\n",
        },
    )
    sync_repo_docs(db, gh, "acme", "widgets")
    with db.session() as s:
        assert s.get(RepoDocsSync, "acme/widgets").scope_version == _SCOPE_VERSION

    # Simulate a row indexed under the previous scope: same tree, old version.
    with db.session() as s:
        s.get(RepoDocsSync, "acme/widgets").scope_version = _SCOPE_VERSION - 1
    gh.file_fetches.clear()

    r = sync_repo_docs(db, gh, "acme", "widgets")
    assert r["status"] == "synced"
    assert sorted(p for p, _ in gh.file_fetches) == [
        "custom_addons/a/README.md",
        "docs/guide.md",
    ]
    with db.session() as s:
        assert s.get(RepoDocsSync, "acme/widgets").scope_version == _SCOPE_VERSION


def test_sync_honors_default_branch(db):
    gh = _FakeGitHub(
        tree=_tree("sha1", ["custom_addons/a/README.md"]),
        files={"custom_addons/a/README.md": "# H\nb\n"},
        default_branch="dev",
    )
    sync_repo_docs(db, gh, "acme", "widgets")
    assert gh.tree_refs == ["dev"]
    assert all(ref == "dev" for _, ref in gh.file_fetches)


def test_sync_only_fetches_in_scope_files(db):
    gh = _FakeGitHub(
        tree=_tree("sha1", [
            "custom_addons/a/README.md",
            "custom_addons/a/model.py",            # not markdown
            "docs/guide.md",                       # repo-root docs: in scope
            "docs/superpowers/specs/x-design.md",  # agent bookkeeping: never
            "README.md",                           # loose root markdown: out
            "custom_addons/a/CLAUDE.md",           # excluded basename
        ]),
        files={
            "custom_addons/a/README.md": "# H\nb\n",
            "docs/guide.md": "# G\nb\n",
        },
    )
    sync_repo_docs(db, gh, "acme", "widgets")
    assert sorted(p for p, _ in gh.file_fetches) == [
        "custom_addons/a/README.md",
        "docs/guide.md",
    ]


def test_sync_truncated_records_ops_event(db):
    gh = _FakeGitHub(
        tree=_tree("sha1", ["custom_addons/a/README.md"], truncated=True),
        files={"custom_addons/a/README.md": "# H\nb\n"},
    )
    r = sync_repo_docs(db, gh, "acme", "widgets")
    assert r["status"] == "synced"
    assert len(_ops(db, "tree_truncated")) == 1


def test_sync_caps_file_count(db):
    paths = [f"custom_addons/a/doc{i}.md" for i in range(_MAX_FILES + 10)]
    gh = _FakeGitHub(
        tree=_tree("sha1", paths),
        files={p: "# H\nb\n" for p in paths},
    )
    r = sync_repo_docs(db, gh, "acme", "widgets")
    assert r["status"] == "synced"
    assert len(gh.file_fetches) == _MAX_FILES
    assert len(_ops(db, "files_capped")) == 1


def test_the_cap_drops_the_least_useful_docs_not_the_alphabetical_tail(db):
    """A cap of 50 sorted by path dropped cu_sale/docs/consultant.md while
    keeping cu_approval/CHANGELOG.md — purely because of the starting letter.
    That is why a question about cu_sale's dummy-article block was answered
    from the code: its documentation was never indexed."""
    changelogs = [f"custom_addons/cu_a{i:03d}/CHANGELOG.md" for i in range(_MAX_FILES)]
    wanted = "custom_addons/cu_sale/docs/consultant.md"
    paths = changelogs + [wanted]
    gh = _FakeGitHub(
        tree=_tree("sha1", paths),
        files={p: "# H\nb\n" for p in paths},
    )
    sync_repo_docs(db, gh, "acme", "widgets")

    assert wanted in [path for path, _ref in gh.file_fetches]
    assert len(gh.file_fetches) == _MAX_FILES


def test_doc_priority_ranks_consultant_docs_over_changelogs():
    ordered = sorted(
        [
            "custom_addons/cu_approval/CHANGELOG.md",
            "custom_addons/cu_sale/qualitycheck.md",
            "custom_addons/cu_sale/docs/testguide.md",
            "custom_addons/cu_sale/README.md",
            "custom_addons/cu_sale/docs/consultant.md",
        ],
        key=doc_priority,
    )
    assert ordered == [
        "custom_addons/cu_sale/docs/consultant.md",
        "custom_addons/cu_sale/README.md",
        "custom_addons/cu_sale/docs/testguide.md",
        "custom_addons/cu_sale/qualitycheck.md",
        "custom_addons/cu_approval/CHANGELOG.md",
    ]


def test_doc_priority_is_deterministic_within_a_tier():
    """A reshuffle between syncs would re-index the whole repo for nothing."""
    paths = ["custom_addons/b/docs/consultant.md", "custom_addons/a/docs/consultant.md"]
    assert sorted(paths, key=doc_priority) == sorted(paths)


def test_sync_installation_error_fails_without_writes(db):
    gh = _FakeGitHub(install_error=RuntimeError("app not installed"))
    r = sync_repo_docs(db, gh, "acme", "widgets")
    assert r["status"] == "failed"
    assert _sections(db, "acme/widgets") == []
    assert len(_ops(db, "sync_failed")) == 1
    with db.session() as s:
        assert s.get(RepoDocsSync, "acme/widgets") is None


def test_sync_all_fetches_failing_keeps_existing_index(db):
    gh = _FakeGitHub(
        tree=_tree("sha1", ["custom_addons/a/README.md"]),
        files={"custom_addons/a/README.md": "# Good\nbody\n"},
    )
    sync_repo_docs(db, gh, "acme", "widgets")
    assert len(_sections(db, "acme/widgets")) == 1

    # New tree, but every fetch now errors → must NOT wipe the good index.
    gh.tree = _tree("sha2", ["custom_addons/a/README.md"])
    gh.file_errors = {"custom_addons/a/README.md"}
    r = sync_repo_docs(db, gh, "acme", "widgets")
    assert r["status"] == "failed"
    assert len(_sections(db, "acme/widgets")) == 1  # old section still there
    # The failed exit still surfaces the per-file failures, not just sync_failed.
    assert len(_ops(db, "files_failed")) == 1


def test_sync_empty_in_scope_empties_index(db):
    gh = _FakeGitHub(
        tree=_tree("sha1", ["custom_addons/a/README.md"]),
        files={"custom_addons/a/README.md": "# H\nb\n"},
    )
    sync_repo_docs(db, gh, "acme", "widgets")
    assert len(_sections(db, "acme/widgets")) == 1

    gh.tree = _tree("sha2", [])  # repo removed all its docs
    r = sync_repo_docs(db, gh, "acme", "widgets")
    assert r["status"] == "synced" and r["sections"] == 0
    assert _sections(db, "acme/widgets") == []


def test_sync_lowercases_repo_key(db):
    gh = _FakeGitHub(
        tree=_tree("sha1", ["custom_addons/a/README.md"]),
        files={"custom_addons/a/README.md": "# H\nb\n"},
    )
    sync_repo_docs(db, gh, "Acme", "Widgets")
    assert len(_sections(db, "acme/widgets")) == 1
