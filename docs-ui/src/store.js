// Shared reactive state (no Pinia needed for this size). Repo list loads once.
// Per repo, branches + the doc tree load lazily on first expand; the tree
// reloads when the selected branch changes. Typing a filter eagerly loads every
// repo's tree so search spans all repos.

import { reactive } from 'vue'
import * as api from './api.js'

export const store = reactive({
  repos: [],
  reposLoading: false,
  reposError: '',
  branches: {}, // repoId -> { items:[{name,sha,is_default}], loading, error, loaded }
  selectedRef: {}, // repoId -> branch name
  trees: {}, // repoId -> { entries, truncated, loading, error, loaded }
  filter: '',
})

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

export async function setBranch(repoId, name) {
  store.selectedRef[repoId] = name
  await loadTree(repoId, { force: true })
}

export function loadAllTrees() {
  for (const r of store.repos) loadTree(r.id)
}
