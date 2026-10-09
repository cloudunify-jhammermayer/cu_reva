"""Response models for the /repo-docs surface (consultant docs site)."""

from __future__ import annotations

from pydantic import BaseModel


class DocsRepo(BaseModel):
    id: int
    full_name: str
    owner: str
    name: str
    default_branch: str


class DocsRepoList(BaseModel):
    items: list[DocsRepo]
    total: int


class DocTreeEntry(BaseModel):
    path: str
    size: int | None = None


class DocTree(BaseModel):
    repository_id: int
    ref: str
    entries: list[DocTreeEntry]
    truncated: bool


class DocTitles(BaseModel):
    repository_id: int
    ref: str
    titles: dict[str, str]  # path -> display title; docs without a heading are absent


class DocFile(BaseModel):
    repository_id: int
    path: str
    ref: str
    content: str


class DocBranch(BaseModel):
    name: str
    sha: str
    is_default: bool


class DocBranchList(BaseModel):
    repository_id: int
    default_branch: str
    items: list[DocBranch]


class DocSearchHit(BaseModel):
    path: str
    snippet: str


class DocSearch(BaseModel):
    repository_id: int
    ref: str
    q: str
    items: list[DocSearchHit]


# --- product page (Internal modules) -------------------------------------------


class ProductRepoRef(BaseModel):
    repository_id: int
    full_name: str
    owner: str
    name: str
    html_url: str


class ProductList(BaseModel):
    items: list[ProductRepoRef]


class ModuleVersion(BaseModel):
    status: str                      # available | planned | discontinued
    version: str | None = None
    eta: str | None = None
    note: str | None = None
    manifest_error: str | None = None
    readme_path: str | None = None
    depends: list[str] = []              # manifest `depends`
    python_deps: list[str] = []          # manifest `external_dependencies.python`
    updated_at: str | None = None        # ISO date of the last commit touching the module on this branch


class ProductModule(BaseModel):
    module: str
    name: str | None = None
    tldr: str | None = None
    owner: str | None = None
    price: str | int | float | None = None   # "free" | EUR amount, one-time
    subscription: int | float | None = None  # EUR per `per`
    per: str | None = None                   # month | year
    features: list[str] = []
    has_yml_entry: bool
    versions: dict[str, ModuleVersion]


class RepoIssue(BaseModel):
    number: int
    title: str
    url: str
    state: str                        # in_progress | planned | wont_do
    assignee: str | None = None
    pr_number: int | None = None
    milestone: str | None = None
    created_at: str
    closed_at: str | None = None


class ProductRepo(ProductRepoRef):
    loaded_at: str
    branches: list[str]
    ignored_branches: list[str]
    modules: list[ProductModule]
    issues: list[RepoIssue]
    warnings: list[str]
