// Shared reactive state (no Pinia needed for this size). Repo list loads once.
// Per repo, branches + the doc tree load lazily on first expand; the tree
// reloads when the selected branch changes. Every repo's tree is loaded up
// front (one cached call each) so the sidebar knows which repos carry docs and
// the filter spans all of them; titles load only for a repo the user opened.

import { reactive } from 'vue'
import * as api from './api.js'

export const store = reactive({
  repos: [],
  reposLoading: false,
  reposError: '',
  branches: {}, // repoId -> { items:[{name,sha,is_default}], loading, error, loaded }
  selectedRef: {}, // repoId -> branch name
  trees: {}, // repoId -> { entries, truncated, loading, error, loaded }
  titles: {}, // repoId -> { ref, map: { path: title } }
  contentHits: {}, // repoId -> { q, snippets: { path: line } }  (full-text matches for the live filter)
  filter: '',
})

const _searchTimers = {}

// Debounced full-text search for one repo; merged into its tree by RepoTree.
export function searchContent(repoId, q, ref) {
  clearTimeout(_searchTimers[repoId])
  _searchTimers[repoId] = setTimeout(async () => {
    const query = q.trim()
    if (store.filter.trim() !== query) return // stale
    try {
      const data = await api.searchDocs(repoId, query, ref)
      store.contentHits[repoId] = {
        q: query,
        snippets: Object.fromEntries(data.items.map((i) => [i.path, i.snippet])),
      }
    } catch { /* ignore search errors — filename filter still works */ }
  }, 350)
}

// Every loaded doc across all repos — backs the Ctrl+K quick-open palette.
export function allLoadedDocs() {
  const out = []
  for (const repo of store.repos) {
    const t = store.trees[repo.id]
    if (!t?.entries) continue
    const ref = store.selectedRef[repo.id] || repo.default_branch
    for (const e of t.entries) {
      out.push({ repoId: repo.id, repoName: repo.full_name, path: e.path, ref })
    }
  }
  return out
}

export async function loadRepos() {
  store.reposLoading = true
  store.reposError = ''
  try {
    const data = await api.listRepos()
    store.repos = data.items
  } catch (e) {
    store.reposError = String(e.message || e)
  } finally {
    store.reposLoading = false
  }
}

export async function loadBranches(repoId) {
  const ex = store.branches[repoId]
  if (ex && (ex.loaded || ex.loading)) return
  store.branches[repoId] = { items: [], loading: true, error: '', loaded: false }
  try {
    const data = await api.getBranches(repoId)
    store.branches[repoId] = { items: data.items, loading: false, error: '', loaded: true }
    if (!store.selectedRef[repoId]) store.selectedRef[repoId] = data.default_branch
  } catch (e) {
    store.branches[repoId] = { items: [], loading: false, error: String(e.message || e), loaded: false }
  }
}

function shaForRef(repoId, ref) {
  const b = store.branches[repoId]?.items?.find((x) => x.name === ref)
  return b ? b.sha : ref // fall back: ref may already be a sha
}

export async function loadTree(repoId, { force = false } = {}) {
  const ex = store.trees[repoId]
  if (!force && ex && (ex.loaded || ex.loading)) return
  await loadBranches(repoId)
  const ref = store.selectedRef[repoId]
  store.trees[repoId] = { entries: [], truncated: false, loading: true, error: '', loaded: false }
  try {
    const data = await api.getTree(repoId, shaForRef(repoId, ref))
    store.trees[repoId] = {
      entries: data.entries,
      truncated: data.truncated,
      loading: false,
      error: '',
      loaded: true,
    }
  } catch (e) {
    store.trees[repoId] = {
      entries: [],
      truncated: false,
      loading: false,
      error: String(e.message || e),
      loaded: false,
    }
  }
}

// Doc headings for the sidebar labels. Best-effort: on failure the tree keeps
// showing filenames.
export async function loadTitles(repoId) {
  await loadBranches(repoId)
  const ref = store.selectedRef[repoId]
  if (store.titles[repoId]?.ref === ref) return
  store.titles[repoId] = { ref, map: {} }
  try {
    const data = await api.getTitles(repoId, shaForRef(repoId, ref))
    if (store.titles[repoId]?.ref === ref) store.titles[repoId] = { ref, map: data.titles }
  } catch {
    if (store.titles[repoId]?.ref === ref) delete store.titles[repoId] // retry on next open
  }
}

export async function setBranch(repoId, name) {
  store.selectedRef[repoId] = name
  await loadTree(repoId, { force: true })
}

export function loadAllTrees() {
  for (const r of store.repos) loadTree(r.id)
}
