<script setup>
import { ref, computed, watch } from 'vue'
import { store } from '../store.js'
import { route, navigate } from '../location.js'
import * as api from '../api.js'
import { renderMarkdown } from '../markdown.js'

const html = ref('')
const loading = ref(false)
const error = ref('')

const repo = computed(() => store.repos.find((r) => r.id === route.value.repoId))
const path = computed(() => route.value.path)
const branch = computed(() => route.value.ref || repo.value?.default_branch)
const ghUrl = computed(() =>
  repo.value
    ? `https://github.com/${repo.value.owner}/${repo.value.name}/blob/${branch.value}/${path.value}`
    : '',
)

async function load() {
  const { repoId, path: filePath, ref: routeRef } = route.value
  if (!repoId || !filePath) return
  loading.value = true
  error.value = ''
  html.value = ''
  try {
    const r = repo.value
    const useRef = routeRef || r?.default_branch
    const data = await api.getFile(repoId, filePath, useRef)
    html.value = renderMarkdown(data.content, {
      repoId,
      path: filePath,
      owner: r?.owner,
      name: r?.name,
      branch: useRef,
    })
  } catch (e) {
    error.value = String(e.message || e)
  } finally {
    loading.value = false
  }
}

watch(route, load, { immediate: true })

// Intercept clicks on rewritten in-app doc links (set in markdown.js); keep the
// current branch.
function onClick(ev) {
  const a = ev.target.closest('a[data-doc-path]')
  if (!a) return
  ev.preventDefault()
  navigate(route.value.repoId, a.getAttribute('data-doc-path'), route.value.ref)
}
</script>

<template>
  <article class="doc">
    <div class="crumbs" v-if="repo">
      <span class="crumb-repo">{{ repo.full_name }}</span>
      <span class="crumb-branch">⎇ {{ branch }}</span>
      <span class="crumb-sep">/</span>
      <span class="crumb-path">{{ path }}</span>
      <a class="gh" :href="ghUrl" target="_blank" rel="noopener noreferrer">View on GitHub ↗</a>
    </div>
    <p v-if="loading" class="muted">Loading…</p>
    <p v-else-if="error" class="error">{{ error }}</p>
    <!-- html is DOMPurify-sanitized in renderMarkdown before it reaches v-html -->
    <div v-else class="markdown-body" v-html="html" @click="onClick"></div>
  </article>
</template>
