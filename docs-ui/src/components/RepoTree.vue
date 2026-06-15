<script setup>
import { ref, computed, watch } from 'vue'
import { store, loadTree, setBranch, searchContent } from '../store.js'
import { buildDocTree } from '../tree.js'
import DocTreeNode from './DocTreeNode.vue'

const props = defineProps({ repo: { type: Object, required: true } })

const manualOpen = ref(false)
const tree = computed(() => store.trees[props.repo.id])
const branches = computed(() => store.branches[props.repo.id])
const selectedRef = computed(() => store.selectedRef[props.repo.id] || props.repo.default_branch)
const filtering = computed(() => store.filter.trim() !== '')

// Files matching the filter — by path, plus full-text hits from the backend.
const filteredEntries = computed(() => {
  const entries = tree.value?.entries ?? []
  const q = store.filter.trim()
  if (!q) return entries
  const f = q.toLowerCase()
  const hits = store.contentHits[props.repo.id]
  const contentPaths = hits && hits.q === q ? new Set(hits.paths) : null
  return entries.filter((e) => e.path.toLowerCase().includes(f) || contentPaths?.has(e.path))
})
const nodes = computed(() => buildDocTree(filteredEntries.value))

const open = computed(() => manualOpen.value || (filtering.value && nodes.value.length > 0))

function toggle() {
  manualOpen.value = !manualOpen.value
  if (manualOpen.value) loadTree(props.repo.id)
}

function onBranchChange(e) {
  setBranch(props.repo.id, e.target.value)
}

// Run a (debounced) full-text search whenever this repo is open and the query
// changes. Guarded so content-hit updates don't re-trigger it.
watch(
  () => (open.value ? store.filter.trim() : ''),
  (q) => {
    if (q.length >= 2) searchContent(props.repo.id, q, selectedRef.value)
  },
)
</script>

<template>
  <div class="repo">
    <button class="repo-name" :class="{ empty: tree?.loaded && !tree.entries.length }" @click="toggle">
      <span class="chev">{{ open ? '▾' : '▸' }}</span>
      <span class="repo-label">{{ repo.full_name }}</span>
      <span v-if="tree?.loaded" class="repo-count">{{ tree.entries.length || 'no docs' }}</span>
    </button>
    <div v-if="open" class="files">
      <div v-if="branches?.items?.length" class="branch-row">
        <span class="branch-ico">⎇</span>
        <select class="branch-select" :value="selectedRef" @change="onBranchChange">
          <option v-for="b in branches.items" :key="b.name" :value="b.name">
            {{ b.name }}{{ b.is_default ? ' (default)' : '' }}
          </option>
        </select>
      </div>
      <p v-else-if="branches?.loading" class="muted">Loading branches…</p>

      <p v-if="tree?.loading" class="muted">Loading…</p>
      <p v-else-if="tree?.error" class="error">{{ tree.error }}</p>
      <template v-else>
        <p v-if="tree?.truncated" class="muted warn">⚠ tree truncated by GitHub</p>
        <p v-if="!nodes.length" class="muted">{{ filtering ? 'No matches.' : 'No docs.' }}</p>
        <DocTreeNode
          v-for="node in nodes"
          :key="node.path || node.name"
          :node="node"
          :repo-id="repo.id"
          :branch-ref="selectedRef"
          :force-open="filtering"
        />
      </template>
    </div>
  </div>
</template>
