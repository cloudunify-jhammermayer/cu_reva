<script setup>
import { onMounted, watch, computed } from 'vue'
import Sidebar from './components/Sidebar.vue'
import DocView from './components/DocView.vue'
import { store, loadRepos, loadAllTrees } from './store.js'
import { route } from './location.js'

onMounted(loadRepos)

// Typing a filter pulls every repo's tree so search spans all repos, not just
// the ones already expanded.
watch(() => store.filter, (f) => {
  if (f.trim()) loadAllTrees()
})

const hasSelection = computed(() => route.value.repoId && route.value.path)
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <header class="brand"><h1>REVA Docs</h1></header>
      <input class="search" v-model="store.filter" type="search" placeholder="Filter docs…" />
      <p v-if="store.reposError" class="error">{{ store.reposError }}</p>
      <Sidebar />
    </aside>
    <main class="content">
      <DocView v-if="hasSelection" />
      <div v-else class="placeholder">
        <p>Select a document from the sidebar.</p>
      </div>
    </main>
  </div>
</template>
