// Minimal query-param router: `?repo=<id>&path=<file>&ref=<branch>` for a doc,
// `?page=<name>` for a standalone page (see PageSwitch.vue). Query-param
// (not hash) routing keeps in-doc `#heading` anchors working and lets a plain
// static host serve index.html for every visit with no rewrite rules.

import { ref } from 'vue'

function parse() {
  const q = new URLSearchParams(window.location.search)
  return {
    repoId: Number(q.get('repo')) || null,
    path: q.get('path') || null,
    ref: q.get('ref') || null,
    page: q.get('page') || null,
  }
}

export const route = ref(parse())

window.addEventListener('popstate', () => {
  route.value = parse()
})

export function navigate(repoId, path, branchRef) {
  const q = new URLSearchParams()
  if (repoId) q.set('repo', String(repoId))
  if (path) q.set('path', path)
  if (branchRef) q.set('ref', branchRef)
  window.history.pushState({}, '', `${window.location.pathname}?${q}`)
  route.value = parse()
}

// Switch to a standalone page, or back to the doc browser with no argument.
export function goPage(page) {
  const q = page ? `?${new URLSearchParams({ page })}` : ''
  window.history.pushState({}, '', `${window.location.pathname}${q}`)
  route.value = parse()
}
