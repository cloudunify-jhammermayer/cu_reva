<script setup>
import { store } from '../store.js'
import RepoTree from './RepoTree.vue'
</script>

<template>
  <nav class="repos">
    <p v-if="store.reposLoading" class="muted">Loading repos…</p>
    <RepoTree v-for="repo in store.repos" :key="repo.id" :repo="repo" />
    <p v-if="!store.reposLoading && !store.repos.length && !store.reposError" class="muted">
      No repositories.
    </p>
  </nav>
</template>
