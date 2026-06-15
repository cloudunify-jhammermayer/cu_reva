<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { loadAllTrees, allLoadedDocs } from '../store.js'
import { navigate } from '../location.js'

const emit = defineEmits(['close'])
const query = ref('')
const index = ref(0)
const inputEl = ref(null)

onMounted(() => {
  loadAllTrees() // make every repo's docs searchable
  inputEl.value?.focus()
})

const results = computed(() => {
  const q = query.value.trim().toLowerCase()
  const docs = allLoadedDocs()
  const matched = q
    ? docs.filter((d) => d.path.toLowerCase().includes(q) || d.repoName.toLowerCase().includes(q))
    : docs
  return matched.slice(0, 50)
})

watch(results, () => { index.value = 0 })

function move(delta) {
  const n = results.value.length
  if (n) index.value = (index.value + delta + n) % n
}
function choose(doc) {
  if (!doc) return
  navigate(doc.repoId, doc.path, doc.ref)
  emit('close')
}
function onKey(e) {
  if (e.key === 'ArrowDown') { e.preventDefault(); move(1) }
  else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1) }
  else if (e.key === 'Enter') { e.preventDefault(); choose(results.value[index.value]) }
  else if (e.key === 'Escape') { e.preventDefault(); emit('close') }
}
</script>

<template>
  <div class="palette-overlay" @click.self="emit('close')">
    <div class="palette">
      <input
        ref="inputEl"
        v-model="query"
        class="palette-input"
        type="text"
        placeholder="Search docs across all repos…"
        @keydown="onKey"
      />
      <div class="palette-results">
        <p v-if="!results.length" class="muted">No matches yet — repos load as you type.</p>
        <button
          v-for="(d, i) in results"
          :key="d.repoId + ':' + d.path"
          class="palette-item"
          :class="{ active: i === index }"
          @click="choose(d)"
          @mouseenter="index = i"
        >
          <span class="palette-path">{{ d.path.replace(/^custom_addons\//, '') }}</span>
          <span class="palette-repo">{{ d.repoName }}</span>
        </button>
      </div>
    </div>
  </div>
</template>
