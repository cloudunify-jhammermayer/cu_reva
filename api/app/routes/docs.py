"""Read-only docs browser surface for consultants (`/repo-docs`).

Pulls Markdown (and doc-embedded images) live from each repo's default branch
via the GitHub Contents/Trees API — the api service has no repo cache on disk,
and live reads are always the default-branch truth.

Authentication is handled at the edge: Cloudflare Access gates
`reva.dev.cloudunify.org/*` before requests reach the origin, so this surface
carries no app-layer auth (and stays separate from /api/v1's machine API key).

Any consultant past Cloudflare Access can browse the docs of every registered
repo; there is no per-repo authorization (that matches the goal — one internal
docs site across all repos).
"""

from __future__ import annotations

import mimetypes
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.dependencies import get_db, get_github_client
from app.doc_cache import branches_cache, file_cache, tree_cache
from app.queries import repos as repo_q
from app.schemas.docs import (
    DocBranchList,
    DocFile,
    DocSearch,
    DocsRepo,
    DocsRepoList,
    DocTree,
)
from reva.db.engine import Database
from reva.db.repo_lookup import get_repo_meta
from reva.errors import PermanentError, TransientError
# Markdown served as text through /file; the doc scope (DOC_EXTENSIONS +
# in_scope — custom addons plus the repo-root docs/ folder) is shared with
# ticket-analysis retrieval — one definition of "the repo's docs"
# (reva/repo_docs.py).
from reva.repo_docs import DOC_EXTENSIONS, in_scope

router = APIRouter()

# Everything the docs embed (images, diagrams, PDFs) served as bytes through
# /raw. Anything else is rejected so the surface stays "docs + their assets",
# not an arbitrary source-file proxy.
ASSET_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif", ".ico", ".bmp", ".pdf",
)
# Bounds for full-text search (first call fetches files; cached thereafter).
MAX_SEARCH_FILES = 300
MAX_SEARCH_RESULTS = 50


def _safe_path(path: str) -> str:
    """Reject empty, absolute, or parent-traversal paths before they reach the
    GitHub API. (GitHub scopes contents to the repo anyway — this is defense in
    depth and a clearer 422 than a malformed upstream request.)"""
    p = path.strip()
    if not p or p.startswith("/") or ".." in p.split("/"):
        raise HTTPException(status_code=422, detail="Invalid path")
    return p


def _meta_and_token(db: Database, github, repository_id: int):
    try:
        meta = get_repo_meta(db, repository_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"Repository {repository_id} not found")
    token = github.get_installation_token(meta["installation_id"])
    return meta, token


def _cached_tree(github, repository_id, owner, name, ref, token) -> dict:
    """{entries, truncated} for a repo+ref, cached. Raises Permanent/Transient
    on the underlying GitHub call (caller maps to 404/502)."""
    key = (repository_id, ref)
    hit = tree_cache.get(key)
    if hit is not None:
        return hit
    tree = github.get_tree(token, owner, name, ref)
    entries = sorted(
        (
            {"path": e["path"], "size": e.get("size")}
            for e in tree.get("tree", [])
            if e.get("type") == "blob" and in_scope(e["path"])
        ),
        key=lambda e: e["path"],
    )
    result = {"entries": entries, "truncated": bool(tree.get("truncated"))}
    tree_cache.set(key, result)
    return result


def _cached_file(github, repository_id, owner, name, path, ref, token) -> str | None:
    """Markdown text for a file, cached. None on 404 (not cached)."""
    key = (repository_id, ref, path)
    hit = file_cache.get(key)
    if hit is not None:
        return hit
    content = github.get_file_content(token, owner, name, path, ref)
    if content is not None:
        file_cache.set(key, content)
    return content


def _snippet(content: str | None, q_lower: str) -> str:
    """First content line containing the query, trimmed — '' if only the
    filename matched."""
    if not content:
        return ""
    for line in content.splitlines():
        if q_lower in line.lower():
            return line.strip()[:160]
    return ""


@router.get("/repos", response_model=DocsRepoList)
def list_doc_repos(db: Database = Depends(get_db)) -> dict:
    """Every enabled registered repo. The frontend lists these, then fetches a
    repo's /tree to discover which actually carry docs."""
    items, _ = repo_q.list_repos(db)
    repos = [
        DocsRepo(
            id=it["id"],
            full_name=it["full_name"],
            owner=it["owner"],
            name=it["name"],
            default_branch=it["default_branch"] or "main",
        )
        for it in items
        if it["enabled"]
    ]
    return {"items": repos, "total": len(repos)}


@router.get("/repos/{repository_id}/branches", response_model=DocBranchList)
def doc_branches(
    repository_id: int,
    db: Database = Depends(get_db),
    github=Depends(get_github_client),
) -> dict:
    """Branches for the repo's branch picker — default branch flagged and sorted
    first, then the rest alphabetically."""
    hit = branches_cache.get(repository_id)
    if hit is not None:
        return hit
    meta, token = _meta_and_token(db, github, repository_id)
    try:
        branches = github.get_branches(token, meta["owner"], meta["name"])
    except PermanentError:
        raise HTTPException(status_code=404, detail="Repository branches not found")
    except TransientError:
        raise HTTPException(status_code=502, detail="Upstream GitHub error")
    default = meta["default_branch"]
    # Only surface the long-lived branches in the picker; the default branch is
    # always kept so the doc view can't be left without its truth ref.
    allowed = {"main", "dev", "test", default}
    items = [
        {"name": b["name"], "sha": b["sha"], "is_default": b["name"] == default}
        for b in branches
        if b["name"] in allowed
    ]
    items.sort(key=lambda b: (not b["is_default"], b["name"].lower()))
    result = {"repository_id": repository_id, "default_branch": default, "items": items}
    branches_cache.set(repository_id, result)
    return result


@router.get("/repos/{repository_id}/tree", response_model=DocTree)
def doc_tree(
    repository_id: int,
    ref: str | None = None,
    db: Database = Depends(get_db),
    github=Depends(get_github_client),
) -> dict:
    """All Markdown paths in the repo at `ref` (default: the repo's default
    branch), sorted. `truncated` is passed through so the frontend can warn when
    GitHub capped the tree."""
    meta, token = _meta_and_token(db, github, repository_id)
    ref = ref or meta["default_branch"]
    try:
        result = _cached_tree(github, repository_id, meta["owner"], meta["name"], ref, token)
    except PermanentError:
        raise HTTPException(status_code=404, detail=f"Tree not found for ref {ref!r}")
    except TransientError:
        raise HTTPException(status_code=502, detail="Upstream GitHub error")
    return {"repository_id": repository_id, "ref": ref, **result}


@router.get("/repos/{repository_id}/file", response_model=DocFile)
def doc_file(
    repository_id: int,
    path: str = Query(...),
    ref: str | None = None,
    db: Database = Depends(get_db),
    github=Depends(get_github_client),
) -> dict:
    """Raw Markdown text for one file. Returned as JSON data (not HTML) — the
    frontend renders + sanitizes it (DOMPurify), so no HTML is built here."""
    safe = _safe_path(path)
    if not safe.lower().endswith(DOC_EXTENSIONS):
        raise HTTPException(status_code=415, detail="Only Markdown files are served as text")
    meta, token = _meta_and_token(db, github, repository_id)
    ref = ref or meta["default_branch"]
    try:
        content = _cached_file(github, repository_id, meta["owner"], meta["name"], safe, ref, token)
    except TransientError:
        raise HTTPException(status_code=502, detail="Upstream GitHub error")
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")
    return {"repository_id": repository_id, "path": safe, "ref": ref, "content": content}


@router.get("/repos/{repository_id}/search", response_model=DocSearch)
def doc_search(
    repository_id: int,
    q: str = Query(..., min_length=2),
    ref: str | None = None,
    db: Database = Depends(get_db),
    github=Depends(get_github_client),
) -> dict:
    """Full-text search within one repo+ref: match the query against doc paths
    and contents, returning a snippet per hit. Files are fetched concurrently and
    cached, so the first search of a repo is the slow one."""
    meta, token = _meta_and_token(db, github, repository_id)
    ref = ref or meta["default_branch"]
    owner, name = meta["owner"], meta["name"]
    try:
        tree = _cached_tree(github, repository_id, owner, name, ref, token)
    except PermanentError:
        raise HTTPException(status_code=404, detail=f"Tree not found for ref {ref!r}")
    except TransientError:
        raise HTTPException(status_code=502, detail="Upstream GitHub error")

    ql = q.lower()
    paths = [e["path"] for e in tree["entries"]][:MAX_SEARCH_FILES]

    def check(path):
        try:
            content = _cached_file(github, repository_id, owner, name, path, ref, token)
        except TransientError:
            content = None
        if ql in path.lower() or (content and ql in content.lower()):
            return {"path": path, "snippet": _snippet(content, ql)}
        return None

    items = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for hit in pool.map(check, paths):
            if hit:
                items.append(hit)
                if len(items) >= MAX_SEARCH_RESULTS:
                    break
    return {"repository_id": repository_id, "ref": ref, "q": q, "items": items}


@router.get("/repos/{repository_id}/raw")
def doc_raw(
    repository_id: int,
    path: str = Query(...),
    ref: str | None = None,
    db: Database = Depends(get_db),
    github=Depends(get_github_client),
) -> Response:
    """Bytes of a doc-embedded asset (image/diagram/PDF). Content-Type is guessed
    from the extension. Served with a locked-down CSP + nosniff so a malicious
    SVG navigated directly can't execute script in this origin."""
    safe = _safe_path(path)
    if not safe.lower().endswith(ASSET_EXTENSIONS):
        raise HTTPException(status_code=415, detail="Only doc assets are served as raw bytes")
    meta, token = _meta_and_token(db, github, repository_id)
    ref = ref or meta["default_branch"]
    try:
        data = github.get_raw_file(token, meta["owner"], meta["name"], safe, ref)
    except TransientError:
        raise HTTPException(status_code=502, detail="Upstream GitHub error")
    if data is None:
        raise HTTPException(status_code=404, detail="File not found")
    media_type = mimetypes.guess_type(safe)[0] or "application/octet-stream"
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )
