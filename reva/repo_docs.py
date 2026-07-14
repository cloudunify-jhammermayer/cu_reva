"""Customer-repo docs retrieval: index each repo's custom-addon markdown docs
section-level into Postgres and search them for ticket-analysis grounding.

Scope (`in_scope`) is the single definition of "the repo's docs", shared with
the consultant docs browser (`api/app/routes/docs.py` imports it from here).
The sync is lazy (see `sync_repo_docs`): it reads from the repo's DEFAULT
branch via the GitHub API, no clone.
"""

from __future__ import annotations

import re
import zlib
from pathlib import PurePosixPath

import structlog
from sqlalchemy import func, or_, select, text

from reva.db import writers
from reva.db.engine import Database
from reva.db.models import RepoDocSection, RepoDocsSync
from reva.odoo_registry import DocSection

logger = structlog.get_logger()

# Two-int advisory-lock namespace ("RDOC"), disjoint from the single-arg budget
# lock. classid is fixed; objid is a per-repo crc32 so different repos never
# skip each other's refresh.
_LOCK_CLASSID = 0x52444F43

# Markdown scope — the consultant docs browser's definition (custom addons only;
# CLAUDE.md is agent instructions, not docs). docs.py imports these back.
DOC_EXTENSIONS = (".md", ".markdown")
SCOPE_PREFIXES = ("custom_addons/", "custom-addons/")
EXCLUDED_BASENAMES = ("CLAUDE.md",)

_MAX_SECTION_CHARS = 2000  # mirrors reva/odoo_registry
_MAX_FILES = 50
_MAX_FILE_CHARS = 100_000

_ATX_RE = re.compile(r"^(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def in_scope(path: str) -> bool:
    """True for a markdown doc under a custom-addons prefix that isn't CLAUDE.md."""
    return (
        path.lower().endswith(DOC_EXTENSIONS)
        and path.startswith(SCOPE_PREFIXES)
        and not path.endswith(tuple("/" + b for b in EXCLUDED_BASENAMES))
    )


def _slugify(title: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in title.lower()).strip("-")


def split_markdown_sections(rel_path: str, text_body: str) -> list[DocSection]:
    """Split one markdown file into ATX-heading-delimited retrieval sections.

    Content before the first heading becomes a section titled with the file
    stem. `#` lines inside fenced code blocks (``` / ~~~) do not split. Section
    bodies are capped at `_MAX_SECTION_CHARS`.
    """
    stem = PurePosixPath(rel_path).stem
    sections: list[DocSection] = []
    title: str | None = None
    body: list[str] = []
    fence: str | None = None  # the fence marker char currently open, if any

    def flush() -> None:
        joined = "\n".join(body).strip()
        if title is None:
            if not joined:  # discard empty preamble
                return
            sections.append(
                DocSection(path=rel_path, anchor=_slugify(stem), title=stem, body=joined[:_MAX_SECTION_CHARS])
            )
            return
        sections.append(
            DocSection(path=rel_path, anchor=_slugify(title), title=title, body=joined[:_MAX_SECTION_CHARS])
        )

    for line in text_body.splitlines():
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            body.append(line)
            continue
        if fence is None:
            heading = _ATX_RE.match(line)
            if heading:
                if title is not None or "\n".join(body).strip():
                    flush()
                body = []
                title = heading.group(2).strip() or stem
                continue
        body.append(line)

    if title is not None or "\n".join(body).strip():
        flush()
    return sections


def search_repo_docs(
    db: Database, repo_full_name: str, terms: list[str], limit: int = 8
) -> list[dict]:
    """Rank a repo's doc sections against English search terms.

    OR-of-terms semantics: a section matches when it matches ANY term, ranked
    by how well it matches overall (`ts_rank` over the OR'd query). The planner
    hands us up to 13 terms+modules — ANDing them all into one
    ``plainto_tsquery`` (the `core_knowledge.search_docs` pattern) would demand
    every term appear in one 2000-char section and near-never match. Words
    WITHIN a term ("quotation template") stay ANDed — one concept.
    Dual-dialect: Postgres FTS, SQLite any-term ilike fallback for unit tests.
    """
    terms = [term.strip() for term in terms if term.strip()]
    if not terms:
        return []
    with db.session() as s:
        if s.get_bind().dialect.name == "postgresql":
            # One tsquery per term, OR'd. Only numbered placeholders are
            # interpolated into the SQL structure; term values stay bound
            # parameters (no injection surface).
            tsq = " || ".join(
                f"plainto_tsquery('english', :t{i})" for i in range(len(terms))
            )
            rows = s.execute(
                text(
                    "SELECT path, anchor, title, body FROM repo_doc_sections "
                    "WHERE repo_full_name = :repo AND "
                    f"to_tsvector('english', title || ' ' || body) @@ ({tsq}) "
                    f"ORDER BY ts_rank(to_tsvector('english', title || ' ' || body), ({tsq})) "
                    "DESC LIMIT :limit"
                ),
                {
                    "repo": repo_full_name,
                    "limit": limit,
                    **{f"t{i}": term for i, term in enumerate(terms)},
                },
            ).all()
            return [
                {"path": row[0], "anchor": row[1], "title": row[2], "body": row[3]}
                for row in rows
            ]

        clauses = [
            or_(
                RepoDocSection.title.ilike(f"%{term}%"),
                RepoDocSection.body.ilike(f"%{term}%"),
            )
            for term in terms
        ]
        rows = s.execute(
            select(RepoDocSection)
            .where(RepoDocSection.repo_full_name == repo_full_name, or_(*clauses))
            .limit(limit)
        ).scalars().all()
        return [
            {"path": row.path, "anchor": row.anchor, "title": row.title, "body": row.body}
            for row in rows
        ]


def _lock_objid(repo_full_name: str) -> int:
    """Signed int4 per-repo advisory-lock key (crc32 folded into int4 range)."""
    unsigned = zlib.crc32(repo_full_name.encode())
    return unsigned - 2**32 if unsigned >= 2**31 else unsigned


def sync_repo_docs(db: Database, github, owner: str, repo: str) -> dict:
    """Bring a repo's doc-section index up to date with its DEFAULT branch.

    Never raises. Returns ``{"status", "sections", "error"}`` where status is:
    ``"fresh"`` (already current, nothing fetched), ``"synced"`` (re-indexed),
    ``"busy"`` (another worker holds the per-repo lock — use the current index),
    ``"failed"`` (GitHub/API error — the existing index is left intact).
    Sync-level degradations are recorded as ops events here.
    """
    repo_key = f"{owner.lower()}/{repo.lower()}"

    # --- resolve default branch + tree (staleness key = the tree's own SHA) ---
    try:
        installation_id = github.get_repo_installation_id(owner, repo)
        token = github.get_installation_token(installation_id)
        default_branch = github.get_repo(token, owner, repo).get("default_branch") or "main"
        tree = github.get_tree(token, owner, repo, default_branch)
    except Exception as exc:
        logger.warning("repo_docs_sync_failed", repo=repo_key, error=str(exc), exc_info=True)
        writers.record_ops_event(db, "repo_docs", "warning", "sync_failed",
            {"repo": repo_key, "error": str(exc)[:300]})
        return {"status": "failed", "sections": None, "error": str(exc)}

    tree_sha = tree.get("sha")
    truncated = bool(tree.get("truncated"))

    # Fast path (no lock): the indexed SHA already matches.
    with db.session() as s:
        row = s.get(RepoDocsSync, repo_key)
        if row is not None and tree_sha is not None and row.tree_sha == tree_sha:
            return {"status": "fresh", "sections": row.sections, "error": None}

    all_paths = sorted(
        e["path"] for e in tree.get("tree", [])
        if e.get("type") == "blob" and in_scope(e["path"])
    )
    capped = len(all_paths) > _MAX_FILES
    paths = all_paths[:_MAX_FILES]

    # --- re-index under a per-repo lock (fetch happens inside the txn so
    #     skip-if-busy is meaningful) ---------------------------------------
    fetch_errors = 0

    def record_caps() -> None:
        # Visibility for caps/failures hit this sync — called on both the
        # failed and the synced exit so a truncated tree or capped file list
        # is never silent (fetch_errors is read at call time).
        if truncated:
            logger.warning("repo_docs_tree_truncated", repo=repo_key)
            writers.record_ops_event(db, "repo_docs", "warning", "tree_truncated", {"repo": repo_key})
        if capped:
            logger.warning("repo_docs_files_capped", repo=repo_key, total=len(all_paths), cap=_MAX_FILES)
            writers.record_ops_event(db, "repo_docs", "warning", "files_capped",
                {"repo": repo_key, "total": len(all_paths), "cap": _MAX_FILES})
        if fetch_errors:
            logger.warning("repo_docs_files_failed", repo=repo_key, count=fetch_errors)
            writers.record_ops_event(db, "repo_docs", "warning", "files_failed",
                {"repo": repo_key, "count": fetch_errors})
    with db.session() as s:
        if s.get_bind().dialect.name == "postgresql":
            got = s.execute(
                text("SELECT pg_try_advisory_xact_lock(:c, :o)"),
                {"c": _LOCK_CLASSID, "o": _lock_objid(repo_key)},
            ).scalar_one()
            if not got:
                logger.info("repo_docs_sync_busy", repo=repo_key)
                return {"status": "busy", "sections": None, "error": None}

        # Re-check under the lock: a concurrent sync may have just committed.
        row = s.get(RepoDocsSync, repo_key)
        if row is not None and tree_sha is not None and row.tree_sha == tree_sha:
            return {"status": "fresh", "sections": row.sections, "error": None}

        sections: list[DocSection] = []
        for path in paths:
            try:
                content = github.get_file_content(token, owner, repo, path, default_branch)
            except Exception as exc:
                fetch_errors += 1
                logger.warning("repo_docs_file_failed", repo=repo_key, path=path, error=str(exc))
                continue
            if not content:
                continue
            sections.extend(split_markdown_sections(path, content[:_MAX_FILE_CHARS]))

        # Never wipe a good index because GitHub flaked mid-sync: only bail when
        # files were listed, none produced sections, AND fetches errored. An
        # empty in-scope list legitimately empties the index.
        if paths and not sections and fetch_errors:
            logger.warning("repo_docs_all_fetches_failed", repo=repo_key, files=len(paths))
            writers.record_ops_event(db, "repo_docs", "warning", "sync_failed",
                {"repo": repo_key, "error": f"all {len(paths)} doc fetches failed"})
            record_caps()
            return {"status": "failed", "sections": None, "error": "all doc fetches failed"}

        s.query(RepoDocSection).filter_by(repo_full_name=repo_key).delete()
        for sec in sections:
            s.add(RepoDocSection(
                repo_full_name=repo_key, path=sec.path, anchor=sec.anchor,
                title=sec.title, body=sec.body,
            ))
        if row is None:
            s.add(RepoDocsSync(
                repo_full_name=repo_key, tree_sha=tree_sha or "",
                files=len(paths), sections=len(sections), truncated=truncated,
            ))
        else:
            row.tree_sha = tree_sha or ""
            row.synced_at = func.now()
            row.files = len(paths)
            row.sections = len(sections)
            row.truncated = truncated

    record_caps()  # post-commit
    return {"status": "synced", "sections": len(sections), "error": None}
