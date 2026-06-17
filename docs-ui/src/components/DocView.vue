<script setup>
import { ref, computed, watch, nextTick } from 'vue'
import { store } from '../store.js'
import { route, navigate } from '../location.js'
import * as api from '../api.js'
import { renderMarkdown } from '../markdown.js'

const html = ref('')
const toc = ref([])
const loading = ref(false)
const error = ref('')
const pendingAnchor = ref('')

const repo = computed(() => store.repos.find((r) => r.id === route.value.repoId))
const path = computed(() => route.value.path)
const branch = computed(() => route.value.ref || repo.value?.default_branch)
const ghUrl = computed(() =>
  repo.value
    ? `https://github.com/${repo.value.owner}/${repo.value.name}/blob/${branch.value}/${path.value}`
    : '',
)

function scrollToId(id) {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

// Print-to-PDF: the @media print stylesheet reformats the page for paper; the
// browser's "Save as PDF" filename comes from document.title, so set a
// meaningful one for the duration of the dialog and restore it afterwards.
function downloadPdf() {
  const prev = document.title
  const r = repo.value
  document.title = r ? `${r.full_name} — ${path.value}` : path.value || prev
  const restore = () => {
    document.title = prev
    window.removeEventListener('afterprint', restore)
  }
  window.addEventListener('afterprint', restore)
  window.print()
}

async function renderMermaid() {
  try {
    const { default: mermaid } = await import('mermaid')
    mermaid.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'strict' })
    await mermaid.run({ querySelector: '.markdown-body .mermaid' })
  } catch { /* a bad diagram shouldn't break the page */ }
}

async function load() {
  const { repoId, path: filePath, ref: routeRef } = route.value
  if (!repoId || !filePath) return
  loading.value = true
  error.value = ''
  html.value = ''
  toc.value = []
  try {
    const r = repo.value
    const useRef = routeRef || r?.default_branch
    const data = await api.getFile(repoId, filePath, useRef)
    const result = renderMarkdown(data.content, {
      repoId,
      path: filePath,
      owner: r?.owner,
      name: r?.name,
      branch: useRef,
    })
    html.value = result.html
    toc.value = result.toc
    await nextTick()
    if (result.hasMermaid) renderMermaid()
    // Cross-doc link that carried a #section — scroll once rendered.
    if (pendingAnchor.value) {
      scrollToId(pendingAnchor.value)
      pendingAnchor.value = ''
    }
  } catch (e) {
    error.value = String(e.message || e)
  } finally {
    loading.value = false
  }
}

watch(route, load, { immediate: true })

function onClick(ev) {
  const a = ev.target.closest('a')
  if (!a) return
  const docPath = a.getAttribute('data-doc-path')
  if (docPath) {
    ev.preventDefault()
    pendingAnchor.value = a.getAttribute('data-doc-anchor') || ''
    navigate(route.value.repoId, docPath, route.value.ref)
    return
  }
  const href = a.getAttribute('href') || ''
  if (href.startsWith('#')) {
    ev.preventDefault()
    scrollToId(href.slice(1))
  }
}
</script>

<template>
  <article class="doc">
    <!-- Print-only header so the saved PDF is self-identifying (hidden on screen). -->
    <div class="print-header" v-if="repo">{{ repo.full_name }} · {{ path }} · ⎇ {{ branch }}</div>
    <div class="crumbs" v-if="repo">
      <span class="crumb-repo">{{ repo.full_name }}</span>
      <span class="crumb-branch">⎇ {{ branch }}</span>
      <span class="crumb-sep">/</span>
      <span class="crumb-path">{{ path }}</span>
      <a class="gh" :href="ghUrl" target="_blank" rel="noopener noreferrer">View on GitHub ↗</a>
      <button class="pdf-btn" type="button" @click="downloadPdf">Download PDF</button>
    </div>
    <p v-if="loading" class="muted">Loading…</p>
    <p v-else-if="error" class="error">{{ error }}</p>
    <template v-else>
      <nav v-if="toc.length >= 3" class="toc">
        <div class="toc-title">On this page</div>
        <a
          v-for="t in toc"
          :key="t.id"
          class="toc-link"
          :class="{ 'toc-sub': t.level === 3 }"
          href="#"
          @click.prevent="scrollToId(t.id)"
          >{{ t.text }}</a
        >
      </nav>
      <!-- html is DOMPurify-sanitized in renderMarkdown before it reaches v-html -->
      <div class="markdown-body" v-html="html" @click="onClick"></div>
    </template>
  </article>
</template>
