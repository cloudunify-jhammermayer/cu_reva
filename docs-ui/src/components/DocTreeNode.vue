<script setup>
import { computed } from 'vue'
import { route, navigate } from '../location.js'
import { store } from '../store.js'
import { ui, toggleOpen } from '../persist.js'

// Recursive: renders a folder (collapsible) or a file link. Vue resolves the
// self-reference by filename (DocTreeNode).
const props = defineProps({
  node: { type: Object, required: true },
  repoId: { type: Number, required: true },
  branchRef: { type: String, required: true },
  forceOpen: { type: Boolean, default: false }, // open everything while filtering
  depth: { type: Number, default: 0 },
})

const openKey = computed(() => `f:${props.repoId}:${props.node.path}`)
const open = computed(() => props.forceOpen || !!ui.open[openKey.value])
const indent = computed(() => ({ paddingLeft: `${8 + props.depth * 14}px` }))
const isActive = computed(
  () =>
    props.node.type === 'file' &&
    route.value.repoId === props.repoId &&
    route.value.path === props.node.path,
)

// The search-hit line, cut into plain / matched parts so the match can be
// marked without ever rendering doc text as HTML.
const snippetParts = computed(() => {
  const text = props.node.snippet
  const q = store.filter.trim().toLowerCase()
  if (!text || !q) return []
  const parts = []
  const lower = text.toLowerCase()
  let from = 0
  for (let i = lower.indexOf(q); i !== -1; i = lower.indexOf(q, from)) {
    if (i > from) parts.push({ text: text.slice(from, i), hit: false })
    parts.push({ text: text.slice(i, i + q.length), hit: true })
    from = i + q.length
  }
  if (from < text.length) parts.push({ text: text.slice(from), hit: false })
  return parts
})
</script>

<template>
  <template v-if="node.type === 'dir'">
    <button class="folder" :style="indent" @click="toggleOpen(openKey)">
      <span class="chev">{{ open ? '▾' : '▸' }}</span>
      <span class="folder-name">{{ node.name }}</span>
    </button>
    <template v-if="open">
      <DocTreeNode
        v-for="child in node.children"
        :key="child.path || child.name"
        :node="child"
        :repo-id="repoId"
        :branch-ref="branchRef"
        :force-open="forceOpen"
        :depth="depth + 1"
      />
    </template>
  </template>

  <a
    v-else
    class="file"
    :class="{ active: isActive }"
    :style="indent"
    :title="node.name"
    :href="`?repo=${repoId}&path=${encodeURIComponent(node.path)}&ref=${encodeURIComponent(branchRef)}`"
    @click.prevent="navigate(repoId, node.path, branchRef)"
  >
    {{ node.title || node.name }}
    <span v-if="snippetParts.length" class="hit-snippet">
      <template v-for="(p, i) in snippetParts" :key="i">
        <mark v-if="p.hit">{{ p.text }}</mark>
        <template v-else>{{ p.text }}</template>
      </template>
    </span>
  </a>
</template>
