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

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.dependencies import get_db, get_github_client
from app.queries import repos as repo_q
from app.schemas.docs import DocBranchList, DocFile, DocsRepo, DocsRepoList, DocTree
from reva.db.engine import Database
from reva.db.repo_lookup import get_repo_meta
from reva.errors import PermanentError, TransientError

router = APIRouter()

# Markdown served as text through /file; everything the docs embed (images,
# diagrams, PDFs) served as bytes through /raw. Anything else is rejected so the
# surface stays "docs + their assets", not an arbitrary source-file proxy.
DOC_EXTENSIONS = (".md", ".markdown")
ASSET_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif", ".ico", ".bmp", ".pdf",
)
# The tree is scoped to addon paths (mirrors REVA's review scope in
# reva/diff_utils.py). Top-level docs/, 3rd_party_addons/, .claude/, etc. are
# intentionally excluded — consultants only want the custom addons' docs.
SCOPE_PREFIXES = ("custom_addons/", "custom-addons/")


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
    meta, token = _meta_and_token(db, github, repository_id)
    try:
        branches = github.get_branches(token, meta["owner"], meta["name"])
    except PermanentError:
        raise HTTPException(status_code=404, detail="Repository branches not found")
    except TransientError:
        raise HTTPException(status_code=502, detail="Upstream GitHub error")
    default = meta["default_branch"]
    items = [
        {"name": b["name"], "sha": b["sha"], "is_default": b["name"] == default}
        for b in branches
    ]
    items.sort(key=lambda b: (not b["is_default"], b["name"].lower()))
    return {"repository_id": repository_id, "default_branch": default, "items": items}


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
        tree = github.get_tree(token, meta["owner"], meta["name"], ref)
    except PermanentError:
        raise HTTPException(status_code=404, detail=f"Tree not found for ref {ref!r}")
    except TransientError:
        raise HTTPException(status_code=502, detail="Upstream GitHub error")
    entries = sorted(
        (
            {"path": e["path"], "size": e.get("size")}
            for e in tree.get("tree", [])
            if e.get("type") == "blob"
            and e["path"].lower().endswith(DOC_EXTENSIONS)
            and e["path"].startswith(SCOPE_PREFIXES)
        ),
        key=lambda e: e["path"],
    )
    return {
        "repository_id": repository_id,
        "ref": ref,
        "entries": entries,
        "truncated": bool(tree.get("truncated")),
    }


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
        content = github.get_file_content(token, meta["owner"], meta["name"], safe, ref)
    except TransientError:
        raise HTTPException(status_code=502, detail="Upstream GitHub error")
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")
    return {"repository_id": repository_id, "path": safe, "ref": ref, "content": content}


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
