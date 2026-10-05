<script setup>
import { onMounted, onUnmounted, watch, computed, ref } from 'vue'
import Sidebar from './components/Sidebar.vue'
import DocView from './components/DocView.vue'
import CommandPalette from './components/CommandPalette.vue'
import PageSwitch from './components/PageSwitch.vue'
import InternalModules from './components/InternalModules.vue'
import { store, loadRepos, loadAllTrees } from './store.js'
import { route } from './location.js'
import { ui } from './persist.js'
import markUrl from './assets/cloudunify-mark.png'

const paletteOpen = ref(false)

function onKeydown(e) {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    paletteOpen.value = true
  }
}

const hasSelection = computed(() => route.value.repoId && route.value.path)

// On a phone the sidebar is a drawer over the doc rather than a column beside
// it: it starts open only when there is no doc to show, closes on navigation,
// and its state is not remembered. On a desktop the choice persists.
const narrowQuery = window.matchMedia('(max-width: 768px)')
const narrow = ref(narrowQuery.matches)
const drawer = ref(!hasSelection.value && !route.value.page)
const onNarrowChange = (e) => { narrow.value = e.matches }

const sidebarShown = computed(() => (narrow.value ? drawer.value : ui.sidebar))
function toggleSidebar() {
  if (narrow.value) drawer.value = !drawer.value
  else ui.sidebar = !ui.sidebar
}
watch(route, () => { drawer.value = false })

onMounted(async () => {
  window.addEventListener('keydown', onKeydown)
  narrowQuery.addEventListener('change', onNarrowChange)
  await loadRepos()
  loadAllTrees()
})
onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  narrowQuery.removeEventListener('change', onNarrowChange)
})
</script>

<template>
  <div class="layout" :class="{ 'sidebar-hidden': !sidebarShown }">
    <div class="backdrop" @click="toggleSidebar"></div>
    <aside class="sidebar">
      <header class="brand">
        <img class="brand-mark" :src="markUrl" alt="Cloudunify" />
        <h1>REVA Docs</h1>
        <button class="kbd-hint" title="Quick open (Ctrl/⌘ K)" @click="paletteOpen = true">⌘K</button>
        <button class="kbd-hint" title="Hide sidebar" aria-label="Hide sidebar" @click="toggleSidebar">«</button>
      </header>
      <PageSwitch />
      <input class="search" v-model="store.filter" type="search" placeholder="Filter docs…" />
      <p v-if="store.reposError" class="error">{{ store.reposError }}</p>
      <Sidebar />
    </aside>
    <main class="content">
      <button class="sidebar-show" title="Show sidebar" aria-label="Show sidebar" @click="toggleSidebar">☰</button>
      <InternalModules v-if="route.page === 'internal-modules'" />
      <DocView v-else-if="hasSelection" />
      <div v-else class="placeholder">
        <p>Select a document, or press <kbd>Ctrl</kbd>+<kbd>K</kbd> to search.</p>
      </div>
    </main>
    <CommandPalette v-if="paletteOpen" @close="paletteOpen = false" />
  </div>
</template>
