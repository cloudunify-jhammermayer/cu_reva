// Per-browser UI state that survives a reload: which repos/folders are
// expanded and whether the sidebar is shown. localStorage only — nothing here
// is worth a round-trip to the server.

import { reactive, watch } from 'vue'

const KEY = 'reva-docs:ui'

function read() {
  try {
    return JSON.parse(localStorage.getItem(KEY)) || {}
  } catch {
    return {} // storage blocked or corrupt — start from defaults
  }
}

const saved = read()

export const ui = reactive({
  open: saved.open || {}, // 'r:<repoId>' | 'f:<repoId>:<dir>' | 'empty' -> true
  sidebar: saved.sidebar ?? true,
})

watch(
  ui,
  () => {
    try {
      localStorage.setItem(KEY, JSON.stringify(ui))
    } catch { /* storage blocked — state just won't persist */ }
  },
  { deep: true },
)

export function toggleOpen(key) {
  if (ui.open[key]) delete ui.open[key]
  else ui.open[key] = true
}
