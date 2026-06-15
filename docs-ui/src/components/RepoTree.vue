<script setup>
import { ref, computed } from 'vue'
import { store, loadTree, setBranch } from '../store.js'
import { route } from '../location.js'
import { buildDocTree } from '../tree.js'
import DocTreeNode from './DocTreeNode.vue'

const props = defineProps({ repo: { type: Object, required: true } })

const manualOpen = ref(false)
const tree = computed(() => store.trees[props.repo.id])
const branches = computed(() => store.branches[props.repo.id])
const selectedRef = computed(() => store.selectedRef[props.repo.id] || props.repo.default_branch)
const filtering = computed(() => store.filter.trim() !== '')

// Files matching the filter (substring on full path), then folded into a tree.
const filteredEntries = computed(() => {
  const entries = tree.value?.entries ?? []
  const f = store.filter.trim().toLowerCase()
  return f ? entries.filter((e) => e.path.toLowerCase().includes(f)) : entries
})
const nodes = computed(() => buildDocTree(filteredEntries.value))

// Open when manually expanded, or when a filter is active and this repo matches.
const open = computed(() => manualOpen.value || (filtering.value && nodes.value.length > 0))

function toggle() {
  manualOpen.value = !manualOpen.value
  if (manualOpen.value) loadTree(props.repo.id)
}

function onBranchChange(e) {
  setBranch(props.repo.id, e.target.value)
}
</script>

<template>
  <div class="repo">
    <button class="repo-name" @click="toggle">
      <span class="chev">{{ open ? '▾' : '▸' }}</span>
      <span class="repo-label">{{ repo.full_name }}</span>
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
