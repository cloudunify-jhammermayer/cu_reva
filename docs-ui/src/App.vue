<script setup>
import { onMounted, onUnmounted, watch, computed, ref } from 'vue'
import Sidebar from './components/Sidebar.vue'
import DocView from './components/DocView.vue'
import CommandPalette from './components/CommandPalette.vue'
import { store, loadRepos, loadAllTrees } from './store.js'
import { route } from './location.js'

const paletteOpen = ref(false)

function onKeydown(e) {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    paletteOpen.value = true
  }
}

onMounted(() => {
  loadRepos()
  window.addEventListener('keydown', onKeydown)
})
onUnmounted(() => window.removeEventListener('keydown', onKeydown))

// Typing a filter pulls every repo's tree so search spans all repos.
watch(() => store.filter, (f) => {
  if (f.trim()) loadAllTrees()
})

const hasSelection = computed(() => route.value.repoId && route.value.path)
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <header class="brand">
        <h1>REVA Docs</h1>
        <button class="kbd-hint" title="Quick open (Ctrl/⌘ K)" @click="paletteOpen = true">⌘K</button>
      </header>
      <input class="search" v-model="store.filter" type="search" placeholder="Filter docs…" />
      <p v-if="store.reposError" class="error">{{ store.reposError }}</p>
      <Sidebar />
    </aside>
    <main class="content">
      <DocView v-if="hasSelection" />
      <div v-else class="placeholder">
        <p>Select a document, or press <kbd>Ctrl</kbd>+<kbd>K</kbd> to search.</p>
      </div>
    </main>
    <CommandPalette v-if="paletteOpen" @close="paletteOpen = false" />
  </div>
</template>
