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
