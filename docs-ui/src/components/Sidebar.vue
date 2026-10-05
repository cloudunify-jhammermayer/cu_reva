<script setup>
import { computed } from 'vue'
import { store } from '../store.js'
import { ui, toggleOpen } from '../persist.js'
import RepoTree from './RepoTree.vue'

// A repo counts as empty only once its tree has loaded with no docs; until
// then (or on a load error) it stays in the main list.
const isEmpty = (r) => {
  const t = store.trees[r.id]
  return !!t?.loaded && !t.entries.length
}
const withDocs = computed(() => store.repos.filter((r) => !isEmpty(r)))
const withoutDocs = computed(() => store.repos.filter(isEmpty))
const filtering = computed(() => store.filter.trim() !== '')
</script>

<template>
  <nav class="repos">
    <p v-if="store.reposLoading" class="muted">Loading repos…</p>
    <RepoTree v-for="repo in withDocs" :key="repo.id" :repo="repo" />
    <p v-if="!store.reposLoading && !store.repos.length && !store.reposError" class="muted">
      No repositories.
    </p>
    <template v-if="withoutDocs.length && !filtering">
      <button class="repo-group" @click="toggleOpen('empty')">
        <span class="chev">{{ ui.open.empty ? '▾' : '▸' }}</span>
        Without docs
        <span class="repo-count">{{ withoutDocs.length }}</span>
      </button>
      <template v-if="ui.open.empty">
        <RepoTree v-for="repo in withoutDocs" :key="repo.id" :repo="repo" />
      </template>
    </template>
  </nav>
</template>
