// Thin client for the REVA docs surface. Same-origin in prod (behind Cloudflare
// Access); proxied to the api container in dev (see vite.config.js).

const BASE = '/repo-docs'

async function getJSON(path) {
  const res = await fetch(path)
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (body?.detail) detail = body.detail
    } catch { /* non-JSON error body */ }
    throw new Error(detail)
  }
  return res.json()
}

const withRef = (params, ref) => {
  if (ref) params.set('ref', ref)
  return params.toString()
}

export const listRepos = () => getJSON(`${BASE}/repos`)

export const getBranches = (repoId) => getJSON(`${BASE}/repos/${repoId}/branches`)

// Tree is fetched by the branch HEAD SHA (the Git Trees API wants a tree-ish).
export const getTree = (repoId, ref) =>
  getJSON(`${BASE}/repos/${repoId}/tree?${withRef(new URLSearchParams(), ref)}`)

// File/raw take the branch NAME — the Contents API resolves it (slashes too).
export const getFile = (repoId, filePath, ref) =>
  getJSON(`${BASE}/repos/${repoId}/file?${withRef(new URLSearchParams({ path: filePath }), ref)}`)

export const rawUrl = (repoId, filePath, ref) =>
  `${BASE}/repos/${repoId}/raw?${withRef(new URLSearchParams({ path: filePath }), ref)}`
