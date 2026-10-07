"""Read-only docs browser surface for consultants (`/repo-docs`).

Pulls doc sources — Markdown and HTML — plus doc-embedded images live from each
repo's default branch via the GitHub Contents/Trees API — the api service has no
repo cache on disk, and live reads are always the default-branch truth.

This router carries no app-layer auth of its own (and stays separate from
/api/v1's machine API key) — it assumes a Cloudflare Access application gates
`/docs` and `/repo-docs` at the edge before requests reach the origin. As of
this writing that Access app has not actually been created
(`docs/ops-debt-runbook-2026-07.md` item 4), so in the current deployment
either path is reachable by anyone with the hostname; nothing in this file
enforces the assumption. Once the Access app exists, any consultant past it
can browse the docs of every registered repo — there is no per-repo
authorization, which matches the goal of one internal docs site across all
repos, but does mean the edge gate is the only gate.
"""

from __future__ import annotations

import html
import mimetypes
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.dependencies import get_db, get_github_client
from app.doc_cache import (
    branches_cache,
    file_cache,
    product_flag_cache,
    products_cache,
    titles_cache,
    tree_cache,
)
from app.queries import repos as repo_q
from app.schemas.docs import (
    DocBranchList,
    DocFile,
    DocSearch,
    DocsRepo,
    DocsRepoList,
    DocTitles,
    DocTree,
    ProductList,
    ProductRepo,
)
from reva.db import writers
from reva.db.engine import Database
from reva.db.repo_lookup import get_repo_meta
from reva.errors import PermanentError, TransientError
from reva.product_catalog import (
    WONT_DO_WINDOW,
    build_branch,
    classify_issues,
    merge_repo,
    module_dirs,
    parse_product_yml,
    select_version_branches,
)
# Docs served as text through /file; the browser's doc scope
# (`browser_in_scope` — markdown under custom addons and the repo-root docs/
# folder, plus HTML inside any docs/ folder) is deliberately WIDER than the
# `in_scope` used for ticket-analysis grounding (reva/repo_docs.py).
from reva.repo_docs import BROWSER_DOC_EXTENSIONS, DOC_EXTENSIONS, browser_in_scope
from reva.repo_config import load_repo_config

router = APIRouter()
logger = structlog.get_logger()

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


def _cached_branches(github, repository_id, owner, name, token) -> list[dict]:
    """Raw `[{"name","sha"}]` for a repo, cached. Raises Permanent/Transient."""
    key = ("raw", repository_id)
    hit = branches_cache.get(key)
    if hit is not None:
        return hit
    branches = github.get_branches(token, owner, name)
    branches_cache.set(key, branches)
    return branches


def _product_flag(github, repository_id, meta, token) -> tuple[bool, list[str]]:
    """`product: true` in .claude-review.yml on the default branch, or on the
    highest version branch when the default branch does not say so (a product
    repo may keep `main` empty). The flag is cached; raises Permanent/Transient.
    Returns (flag, invalid_reasons): reasons for a malformed/invalid config file
    are only collected here (this runs in a thread pool) and recorded by the caller;
    a cached hit returns no reasons, so the event fires once per repo per TTL."""
    hit = product_flag_cache.get(repository_id)
    if hit is not None:
        return hit, []
    owner, name = meta["owner"], meta["name"]
    invalid: list[str] = []
    flag = load_repo_config(github, token, owner, name, meta["default_branch"],
                            on_invalid=invalid.append).product
    if not flag:
        names = [b["name"] for b in _cached_branches(github, repository_id, owner, name, token)]
        read, _ = select_version_branches(names)
        if read:
            flag = load_repo_config(github, token, owner, name, read[0],
                                    on_invalid=invalid.append).product
    product_flag_cache.set(repository_id, flag)
    return flag, invalid


def _degrade(db, repository_id: int, step: str, exc: Exception) -> str:
    """Log + ops event for a GitHub failure the product page works around;
    returns the warning line for the response."""
    logger.warning("product_catalog_degraded", repository_id=repository_id, step=step,
                   error=str(exc))
    writers.record_ops_event(
        db, "docs", "warning", "product_catalog_degraded",
        {"repository_id": repository_id, "step": step, "error": str(exc)[:200]},
    )
    return f"{step}: GitHub error, data may be incomplete"


def _record_invalid(db, repository_id: int, reasons: list[str]) -> list[str]:
    """Ops event per invalid-config reason; returns the warning lines."""
    return [_degrade(db, repository_id, "config_invalid", ValueError(r)) for r in reasons]


def _cached_tree(github, repository_id, owner, name, ref, token, product=False) -> dict:
    """{entries, truncated, addon_roots} for a repo+ref, cached. For a product
    repo the addon roots (top-level dirs with a manifest) widen the doc scope
    and feed the product page. Raises Permanent/Transient (caller maps)."""
    key = (repository_id, ref, product)
    hit = tree_cache.get(key)
    if hit is not None:
        return hit
    tree = github.get_tree(token, owner, name, ref)
    blobs = [e["path"] for e in tree.get("tree", []) if e.get("type") == "blob"]
    roots = tuple(module_dirs(blobs)) if product else ()
    sizes = {e["path"]: e.get("size") for e in tree.get("tree", []) if e.get("type") == "blob"}
    entries = sorted(
        ({"path": p, "size": sizes[p]} for p in blobs if browser_in_scope(p, roots)),
        key=lambda e: e["path"],
    )
    result = {"entries": entries, "truncated": bool(tree.get("truncated")),
              "addon_roots": list(roots)}
    tree_cache.set(key, result)
    return result


def _cached_file(github, repository_id, owner, name, path, ref, token) -> str | None:
    """Doc source text (Markdown or HTML) for a file, cached. None on 404 (not cached)."""
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
    # The query orders by full_name in SQL, where the collation decides whether
    # "POMBERGER" lands before "ast-odoo"; the sidebar wants plain A-Z.
    repos.sort(key=lambda r: r.full_name.lower())
    return {"items": repos, "total": len(repos)}


@router.get("/products", response_model=ProductList)
def list_products(db: Database = Depends(get_db), github=Depends(get_github_client)) -> dict:
    """Enabled repos whose .claude-review.yml says `product: true` — the
    sections of the docs site's Internal modules page. One (cached) config
    read per repo; a repo GitHub cannot answer for is skipped with an ops
    event rather than failing the list."""
    items, _ = repo_q.list_repos(db)
    repos = [it for it in items if it["enabled"]]

    def flagged(it):
        # GitHub only in the pool; the ops-event write happens on the request
        # thread below (the test DB is a single shared SQLite connection).
        meta = {"owner": it["owner"], "name": it["name"],
                "default_branch": it["default_branch"] or "main"}
        try:
            token = github.get_installation_token(it["installation_id"])
            flag, invalid = _product_flag(github, it["id"], meta, token)
            return flag, invalid, None
        except (PermanentError, TransientError) as exc:
            return False, [], exc

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(flagged, repos))
    out = []
    for it, (flag, invalid, exc) in zip(repos, results):
        _record_invalid(db, it["id"], invalid)
        if exc is not None:
            _degrade(db, it["id"], "config", exc)
        elif flag:
            out.append({
                "repository_id": it["id"], "full_name": it["full_name"], "owner": it["owner"],
                "name": it["name"], "html_url": f"https://github.com/{it['owner']}/{it['name']}",
            })
    out.sort(key=lambda r: r["full_name"].lower())
    return {"items": out}


@router.get("/products/{repository_id}", response_model=ProductRepo)
def product_detail(
    repository_id: int,
    db: Database = Depends(get_db),
    github=Depends(get_github_client),
) -> dict:
    """One product repo's section: modules across its three highest version
    branches (manifests + product.yml + README presence) and its issues.
    Every GitHub failure degrades to a warning + ops event so one broken
    branch or a rate-limited issues call never blanks the section."""
    hit = products_cache.get(repository_id)
    if hit is not None:
        return hit
    meta, token = _meta_and_token(db, github, repository_id)
    owner, name = meta["owner"], meta["name"]
    try:
        flag, invalid = _product_flag(github, repository_id, meta, token)
        config_warnings = _record_invalid(db, repository_id, invalid)
        if not flag:
            raise HTTPException(status_code=404, detail="Not a product repo")
    except TransientError:
        raise HTTPException(status_code=502, detail="Upstream GitHub error")
    except PermanentError:
        raise HTTPException(status_code=404, detail="Repository not readable")

    now = datetime.now(timezone.utc)
    warnings: list[str] = list(config_warnings)
    read: list[str] = []
    ignored: list[str] = []
    try:
        names = [b["name"] for b in _cached_branches(github, repository_id, owner, name, token)]
        read, ignored = select_version_branches(names)
    except (PermanentError, TransientError) as exc:
        warnings.append(_degrade(db, repository_id, "branches", exc))

    per_branch: list[tuple[str, dict]] = []
    for branch in read:
        try:
            tree = _cached_tree(github, repository_id, owner, name, branch, token, product=True)
        except (PermanentError, TransientError) as exc:
            warnings.append(_degrade(db, repository_id, f"tree:{branch}", exc))
            continue
        if tree["truncated"]:
            warnings.append(f"{branch}: GitHub truncated the file tree; modules may be missing")
        roots = tree["addon_roots"]
        readmes = {e["path"] for e in tree["entries"]
                   if e["path"].count("/") == 1 and e["path"].endswith("/README.md")}
        failed: list[str] = []

        def fetch(path, _branch=branch, _failed=failed):
            try:
                return _cached_file(github, repository_id, owner, name, path, _branch, token)
            except (PermanentError, TransientError):
                _failed.append(path)
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            texts = list(pool.map(fetch, [f"{r}/__manifest__.py" for r in roots]))
        manifests = dict(zip(roots, texts))
        yml, yml_warnings = parse_product_yml(fetch("product.yml"))
        warnings.extend(f"{branch}: {w}" for w in yml_warnings)
        if failed:
            warnings.append(_degrade(db, repository_id, f"manifests:{branch}",
                                     TransientError(f"{len(failed)} file fetches failed")))
        rows, row_warnings = build_branch(branch, manifests, readmes, yml)
        warnings.extend(row_warnings)
        per_branch.append((branch, rows))

    modules, merge_warnings = merge_repo(per_branch)
    warnings.extend(merge_warnings)

    issues: list[dict] = []
    try:
        since = (now - WONT_DO_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw = github.list_issues(token, owner, name, state="open")
        raw += github.list_issues(token, owner, name, state="closed", since=since)
        prs = github.list_open_pull_requests(token, owner, name)
        issues = classify_issues(raw, prs, now)
    except (PermanentError, TransientError) as exc:
        warnings.append(_degrade(db, repository_id, "issues", exc))

    result = {
        "repository_id": repository_id,
        "full_name": f"{owner}/{name}",
        "owner": owner,
        "name": name,
        "html_url": f"https://github.com/{owner}/{name}",
        "loaded_at": now.isoformat(),
        "branches": read,
        "ignored_branches": ignored,
        "modules": modules,
        "issues": issues,
        "warnings": warnings,
    }
    products_cache.set(repository_id, result)
    return result


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
        branches = _cached_branches(github, repository_id, meta["owner"], meta["name"], token)
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
    return {"repository_id": repository_id, "default_branch": default, "items": items}


@router.get("/repos/{repository_id}/tree", response_model=DocTree)
def doc_tree(
    repository_id: int,
    ref: str | None = None,
    db: Database = Depends(get_db),
    github=Depends(get_github_client),
) -> dict:
    """All doc paths in the repo at `ref` (default: the repo's default
    branch), sorted. `truncated` is passed through so the frontend can warn when
    GitHub capped the tree."""
    meta, token = _meta_and_token(db, github, repository_id)
    ref = ref or meta["default_branch"]
    try:
        product, invalid = _product_flag(github, repository_id, meta, token)
        _record_invalid(db, repository_id, invalid)
        result = _cached_tree(github, repository_id, meta["owner"], meta["name"], ref, token,
                              product=product)
    except PermanentError:
        raise HTTPException(status_code=404, detail=f"Tree not found for ref {ref!r}")
    except TransientError:
        raise HTTPException(status_code=502, detail="Upstream GitHub error")
    return {"repository_id": repository_id, "ref": ref, **result}


_MD_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$")
_HTML_H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")
MAX_TITLE_CHARS = 120


def _doc_title(path: str, content: str | None) -> str:
    """The doc's first top-level heading as plain text — '' when it has none
    (the frontend then falls back to the filename)."""
    if not content:
        return ""
    if path.lower().endswith(BROWSER_DOC_EXTENSIONS):
        m = _HTML_H1.search(content)
        title = html.unescape(_HTML_TAG.sub("", m.group(1))) if m else ""
    else:
        title, fenced = "", False
        for line in content.splitlines():
            if line.lstrip().startswith(("```", "~~~")):
                fenced = not fenced
            elif not fenced and (m := _MD_H1.match(line)):
                title = re.sub(r"[`*]", "", m.group(1))
                break
    return " ".join(title.split())[:MAX_TITLE_CHARS]


@router.get("/repos/{repository_id}/titles", response_model=DocTitles)
def doc_titles(
    repository_id: int,
    ref: str | None = None,
    db: Database = Depends(get_db),
    github=Depends(get_github_client),
) -> dict:
    """Display title (first top-level heading) per doc path, so the sidebar can
    show "Kardex ledger guide" instead of `kardex_ledger.md`. Costs one file
    fetch per doc on a cold cache, like the first /search of a repo — the
    frontend asks only for a repo the user actually opened."""
    meta, token = _meta_and_token(db, github, repository_id)
    ref = ref or meta["default_branch"]
    owner, name = meta["owner"], meta["name"]
    hit = titles_cache.get((repository_id, ref))
    if hit is not None:
        return hit
    try:
        product, invalid = _product_flag(github, repository_id, meta, token)
        _record_invalid(db, repository_id, invalid)
        tree = _cached_tree(github, repository_id, owner, name, ref, token, product=product)
    except PermanentError:
        raise HTTPException(status_code=404, detail=f"Tree not found for ref {ref!r}")
    except TransientError:
        raise HTTPException(status_code=502, detail="Upstream GitHub error")

    paths = [e["path"] for e in tree["entries"]][:MAX_SEARCH_FILES]
    failed: list[str] = []

    def title_of(path):
        try:
            content = _cached_file(github, repository_id, owner, name, path, ref, token)
        except TransientError:
            failed.append(path)
            return ""
        return _doc_title(path, content)

    with ThreadPoolExecutor(max_workers=8) as pool:
        titles = {p: t for p, t in zip(paths, pool.map(title_of, paths)) if t}
    result = {"repository_id": repository_id, "ref": ref, "titles": titles}
    if failed:
        # Those docs keep their filename in the sidebar; leave the result
        # uncached so the next open retries them.
        writers.record_ops_event(
            db, "docs", "warning", "doc_titles_fetch_failed",
            {"repository_id": repository_id, "ref": ref, "failed": len(failed)},
        )
    else:
        titles_cache.set((repository_id, ref), result)
    return result


@router.get("/repos/{repository_id}/file", response_model=DocFile)
def doc_file(
    repository_id: int,
    path: str = Query(...),
    ref: str | None = None,
    db: Database = Depends(get_db),
    github=Depends(get_github_client),
) -> dict:
    """Raw doc source (Markdown or HTML) for one file. Returned as JSON data,
    never as an HTML response — the frontend renders + sanitizes it (DOMPurify),
    so no HTML is built or served as a document here."""
    safe = _safe_path(path)
    # Markdown stays extension-only (unrestricted by folder, as before). HTML is
    # additionally scope-checked — browser_in_scope keeps a manifest stub like
    # custom_addons/cu_x/static/description/index.html out even though its
    # extension matches.
    is_html = safe.lower().endswith(BROWSER_DOC_EXTENSIONS)
    allowed = safe.lower().endswith(DOC_EXTENSIONS) or (is_html and browser_in_scope(safe))
    if not allowed and not is_html:
        raise HTTPException(status_code=415, detail="Only doc files are served as text")
    meta, token = _meta_and_token(db, github, repository_id)
    ref = ref or meta["default_branch"]
    if not allowed:
        # HTML outside the default scope: only a product repo's addon docs/ qualify.
        try:
            flag, invalid = _product_flag(github, repository_id, meta, token)
            _record_invalid(db, repository_id, invalid)
            if flag:
                tree = _cached_tree(github, repository_id, meta["owner"], meta["name"], ref,
                                    token, product=True)
                allowed = browser_in_scope(safe, tuple(tree["addon_roots"]))
        except PermanentError:
            raise HTTPException(status_code=404, detail=f"Tree not found for ref {ref!r}")
        except TransientError:
            raise HTTPException(status_code=502, detail="Upstream GitHub error")
        if not allowed:
            raise HTTPException(status_code=415, detail="Only doc files are served as text")
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
        product, invalid = _product_flag(github, repository_id, meta, token)
        _record_invalid(db, repository_id, invalid)
        tree = _cached_tree(github, repository_id, owner, name, ref, token, product=product)
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
