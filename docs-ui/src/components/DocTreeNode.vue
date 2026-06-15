<script setup>
import { ref, computed } from 'vue'
import { route, navigate } from '../location.js'

// Recursive: renders a folder (collapsible) or a file link. Vue resolves the
// self-reference by filename (DocTreeNode).
const props = defineProps({
  node: { type: Object, required: true },
  repoId: { type: Number, required: true },
  branchRef: { type: String, required: true },
  forceOpen: { type: Boolean, default: false }, // open everything while filtering
  depth: { type: Number, default: 0 },
})

const localOpen = ref(false)
const open = computed(() => props.forceOpen || localOpen.value)
const indent = computed(() => ({ paddingLeft: `${8 + props.depth * 14}px` }))
const isActive = computed(
  () =>
    props.node.type === 'file' &&
    route.value.repoId === props.repoId &&
    route.value.path === props.node.path,
)
</script>

<template>
  <template v-if="node.type === 'dir'">
    <button class="folder" :style="indent" @click="localOpen = !localOpen">
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
    :href="`?repo=${repoId}&path=${encodeURIComponent(node.path)}&ref=${encodeURIComponent(branchRef)}`"
    @click.prevent="navigate(repoId, node.path, branchRef)"
    >{{ node.name }}</a
  >
</template>
