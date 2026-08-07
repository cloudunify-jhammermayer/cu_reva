// Turn the flat list of doc paths from /tree into a nested folder hierarchy.
// The backend scopes to custom_addons/ plus the repo-root docs/ folder; we strip
// the custom_addons segment so addon folders surface at the top level, and keep
// the repo's own docs/ folder there under its own name.

const SCOPE_PREFIXES = ['custom_addons', 'custom-addons']

// Returns an array of nodes:
//   { type: 'dir',  name, children: Node[] }
//   { type: 'file', name, path }      // path = full repo path, for fetching
export function buildDocTree(entries) {
  const root = { dirs: new Map(), files: [] }
  for (const e of entries) {
    let segs = e.path.split('/').filter(Boolean)
    if (SCOPE_PREFIXES.includes(segs[0])) segs = segs.slice(1)
    if (!segs.length) continue
    let node = root
    for (const dir of segs.slice(0, -1)) {
      if (!node.dirs.has(dir)) node.dirs.set(dir, { dirs: new Map(), files: [] })
      node = node.dirs.get(dir)
    }
    node.files.push({ name: segs[segs.length - 1], path: e.path })
  }
  return docsFirst(toNodes(root))
}

// The repo's own docs/ folder is the natural entry point, but addons are named
// cu_* and sort ahead of it — hoist it to the top of the root listing.
function docsFirst(nodes) {
  const i = nodes.findIndex((n) => n.type === 'dir' && n.name === 'docs')
  return i <= 0 ? nodes : [nodes[i], ...nodes.slice(0, i), ...nodes.slice(i + 1)]
}

function toNodes(node) {
  const dirs = [...node.dirs.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([name, child]) => ({ type: 'dir', name, children: toNodes(child) }))
  const files = node.files
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name))
    .map((f) => ({ type: 'file', name: f.name, path: f.path }))
  return [...dirs, ...files] // folders first, then files
}
