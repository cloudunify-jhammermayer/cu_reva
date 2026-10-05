<script setup>
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { store } from '../store.js'
import { route, navigate } from '../location.js'
import * as api from '../api.js'
import { renderMarkdown, renderHtml } from '../markdown.js'

const html = ref('')
const toc = ref([])
const loading = ref(false)
const error = ref('')
const pendingAnchor = ref('')
const docEl = ref(null)
const activeId = ref('') // TOC entry for the section currently at the top

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

// Scrollspy: the active TOC entry is the last heading that has passed the top
// of the scrolling pane (`.content`, not the window).
let scroller = null
let spyQueued = false
function updateActive() {
  spyQueued = false
  if (!scroller || !toc.value.length) return
  const line = scroller.getBoundingClientRect().top + 96
  let current = toc.value[0].id
  for (const t of toc.value) {
    const el = document.getElementById(t.id)
    if (el && el.getBoundingClientRect().top <= line) current = t.id
  }
  activeId.value = current
}
function onScroll() {
  if (spyQueued) return
  spyQueued = true
  requestAnimationFrame(updateActive)
}
onMounted(() => {
  scroller = docEl.value?.closest('.content')
  scroller?.addEventListener('scroll', onScroll, { passive: true })
})
onUnmounted(() => scroller?.removeEventListener('scroll', onScroll))

// Clipboard write + a short visual confirmation on the element that was clicked.
async function copyFrom(el, text) {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    return // no clipboard access (insecure context / denied) — nothing to confirm
  }
  el.classList.add('copied')
  setTimeout(() => el.classList.remove('copied'), 1400)
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
    const render = /\.html?$/i.test(filePath) ? renderHtml : renderMarkdown
    const result = render(data.content, {
      repoId,
      path: filePath,
      owner: r?.owner,
      name: r?.name,
      branch: useRef,
    })
    html.value = result.html
    toc.value = result.toc
    loading.value = false // the body renders only once loading is off — the scroll below needs it in the DOM
    await nextTick()
    if (result.hasMermaid) renderMermaid()
    // A shared section link (…#heading) lands on its section too.
    if (!pendingAnchor.value && window.location.hash) {
      pendingAnchor.value = decodeURIComponent(window.location.hash.slice(1))
    }
    // Cross-doc link that carried a #section — scroll once rendered.
    if (pendingAnchor.value) {
      scrollToId(pendingAnchor.value)
      pendingAnchor.value = ''
    } else if (scroller) {
      scroller.scrollTop = 0
    }
    updateActive()
  } catch (e) {
    error.value = String(e.message || e)
  } finally {
    loading.value = false
  }
}

watch(route, load, { immediate: true })

function onClick(ev) {
  const copyBtn = ev.target.closest('.code-copy')
  if (copyBtn) {
    copyFrom(copyBtn, copyBtn.parentElement.querySelector('code')?.textContent || '')
    return
  }
  const a = ev.target.closest('a')
  if (!a) return
  // Heading anchor: put the section in the address bar and copy that link.
  if (a.classList.contains('heading-anchor')) {
    ev.preventDefault()
    const url = new URL(window.location.href)
    url.hash = a.getAttribute('href')
    window.history.replaceState({}, '', url)
    copyFrom(a, url.href)
    return
  }
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
  <article ref="docEl" class="doc" :class="{ 'has-toc': !loading && !error && toc.length >= 3 }">
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
    <div v-else class="doc-grid">
      <nav v-if="toc.length >= 3" class="toc">
        <div class="toc-title">On this page</div>
        <a
          v-for="t in toc"
          :key="t.id"
          class="toc-link"
          :class="{ 'toc-sub': t.level === 3, active: t.id === activeId }"
          href="#"
          @click.prevent="scrollToId(t.id)"
          >{{ t.text }}</a
        >
      </nav>
      <!-- html is DOMPurify-sanitized in renderMarkdown/renderHtml before it reaches v-html -->
      <div class="markdown-body" v-html="html" @click="onClick"></div>
    </div>
  </article>
</template>
