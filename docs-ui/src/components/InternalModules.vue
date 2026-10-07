<script setup>
// Internal modules: every sellable Cloudunify addon, read live from the product
// repos (spec: docs/superpowers/specs/archive/2026-10-07-product-modules-overview-design.md).
// The list is one cheap call; each repo's detail loads in parallel and renders
// as it arrives, so one slow repo never blocks the page.
import { ref, reactive, onMounted } from 'vue'
import * as api from '../api.js'
import { renderMarkdown } from '../markdown.js'
import { navigate } from '../location.js'
import { ui, toggleOpen } from '../persist.js'

const repos = ref([])        // ProductRepoRef[]
const loading = ref(true)
const error = ref('')
const details = reactive({}) // repoId -> { data, loading, error }
const readmes = reactive({}) // 'm:<repoId>:<module>' -> { branch, path, html, loading, error, none }

onMounted(async () => {
  try {
    const list = await api.listProducts()
    repos.value = list.items
    for (const r of list.items) loadDetail(r)
  } catch (e) {
    error.value = String(e.message || e)
  } finally {
    loading.value = false
  }
})

async function loadDetail(repo) {
  const repoId = repo.repository_id
  details[repoId] = { data: null, loading: true, error: '' }
  try {
    details[repoId] = { data: await api.getProduct(repoId), loading: false, error: '' }
    // Rows left open in a previous session need their README too.
    for (const m of details[repoId].data.modules) if (isOpen(repo, m)) loadReadme(repo, m)
  } catch (e) {
    details[repoId] = { data: null, loading: false, error: String(e.message || e) }
  }
}

const key = (repo, m) => `m:${repo.repository_id}:${m.module}`
const isOpen = (repo, m) => !!ui.open[key(repo, m)]
function toggle(repo, m) {
  toggleOpen(key(repo, m))
  if (isOpen(repo, m)) loadReadme(repo, m)
}

// The README shown inline comes from the highest branch that has one.
function readmeSource(repo, m) {
  for (const b of details[repo.repository_id].data.branches) {
    const v = m.versions[b]
    if (v?.readme_path) return { branch: b, path: v.readme_path }
  }
  return null
}

async function loadReadme(repo, m) {
  const k = key(repo, m)
  if (readmes[k]) return
  const src = readmeSource(repo, m)
  if (!src) {
    readmes[k] = { none: true, html: '', loading: false, error: '' }
    return
  }
  readmes[k] = { ...src, html: '', loading: true, error: '' }
  try {
    const data = await api.getFile(repo.repository_id, src.path, src.branch)
    const result = renderMarkdown(data.content, {
      repoId: repo.repository_id,
      path: src.path,
      owner: repo.owner,
      name: repo.name,
      branch: src.branch,
    })
    readmes[k] = { ...src, html: result.html, loading: false, error: '' }
  } catch (e) {
    readmes[k] = { ...src, html: '', loading: false, error: String(e.message || e) }
  }
}

// In-repo doc links inside a rendered README open in the doc view.
function onReadmeClick(ev, repo, m) {
  const a = ev.target.closest('a')
  const docPath = a?.getAttribute('data-doc-path')
  if (!docPath) return
  ev.preventDefault()
  navigate(repo.repository_id, docPath, readmes[key(repo, m)]?.branch)
}

const price = (p) =>
  p == null ? '—' : p === 'free' ? 'free' : `€ ${Number(p).toLocaleString('de-AT')}`
const allDiscontinued = (repo, m) =>
  Object.keys(m.versions).length > 0 &&
  Object.values(m.versions).every((v) => v.status === 'discontinued')
const discontinuedNote = (m) =>
  Object.values(m.versions).find((v) => v.status === 'discontinued')?.note
const stateLabel = { in_progress: 'in progress', planned: 'planned', wont_do: "won't do" }
const loadedAt = (iso) =>
  new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
const age = (iso) => {
  const days = Math.floor((Date.now() - new Date(iso)) / 86400000)
  return days < 1 ? 'today' : days === 1 ? '1 day ago' : days < 30 ? `${days} days ago`
    : days < 60 ? '1 month ago' : `${Math.floor(days / 30)} months ago`
}
</script>

<template>
  <article class="doc products">
    <div class="markdown-body">
      <h1>Internal modules</h1>
      <p class="lede">
        Every Cloudunify addon we sell, read live from the product repos: one row per module,
        one column per Odoo version branch (the three newest, no backports). Owner, price,
        summary, feature set and status exceptions come from each branch's
        <code>product.yml</code>; versions from the manifests; open issues from GitHub.
      </p>
    </div>

    <p v-if="loading" class="muted">Loading…</p>
    <p v-else-if="error" class="error">{{ error }}</p>
    <p v-else-if="!repos.length" class="muted">
      No product repos yet. A repo joins this page with <code>product: true</code> in its
      <code>.claude-review.yml</code>.
    </p>

    <section v-for="r in repos" :key="r.repository_id" class="product">
      <div class="product-head">
        <h2><a href="#" @click.prevent="navigate(r.repository_id)">{{ r.name }}</a></h2>
        <span class="meta">
          {{ r.full_name }}
          <template v-if="details[r.repository_id]?.data">
            · branches {{ details[r.repository_id].data.branches.join(', ') || 'none' }}
            <template v-if="details[r.repository_id].data.ignored_branches.length">
              ({{ details[r.repository_id].data.ignored_branches.join(', ') }} ignored)
            </template>
            · loaded {{ loadedAt(details[r.repository_id].data.loaded_at) }}
          </template>
          · <a :href="r.html_url" target="_blank" rel="noopener noreferrer">GitHub ↗</a>
        </span>
      </div>

      <p v-if="details[r.repository_id]?.loading" class="muted">Loading…</p>
      <p v-else-if="details[r.repository_id]?.error" class="error">
        {{ details[r.repository_id].error }}
      </p>
      <template v-else-if="details[r.repository_id]?.data">
        <p
          v-for="w in details[r.repository_id].data.warnings"
          :key="w"
          class="warning"
        >{{ w }}</p>

        <p v-if="!details[r.repository_id].data.modules.length" class="muted">
          No modules found on the version branches.
        </p>
        <div v-else class="tablewrap">
          <table>
            <thead>
              <tr>
                <th>Module</th>
                <th>Summary</th>
                <th>Owner</th>
                <th class="price">Price (one-time, net EUR)</th>
                <th v-for="b in details[r.repository_id].data.branches" :key="b" class="ver">
                  {{ b }}
                </th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <template v-for="m in details[r.repository_id].data.modules" :key="m.module">
                <tr
                  class="mod"
                  :class="{ off: allDiscontinued(details[r.repository_id].data, m), noyml: !m.has_yml_entry }"
                >
                  <td class="name">
                    {{ m.name || m.module }}
                    <span class="tech">{{ m.module }}</span>
                    <span v-if="!m.has_yml_entry" class="tech">no product.yml entry</span>
                  </td>
                  <td class="tldr">
                    <template v-if="allDiscontinued(details[r.repository_id].data, m)">
                      Discontinued<span v-if="discontinuedNote(m)">: {{ discontinuedNote(m) }}</span>
                    </template>
                    <template v-else>{{ m.tldr || '—' }}</template>
                  </td>
                  <td class="owner">{{ m.owner || '—' }}</td>
                  <td class="price" :class="{ free: m.price === 'free' }">{{ price(m.price) }}</td>
                  <td v-for="b in details[r.repository_id].data.branches" :key="b" class="ver">
                    <template v-if="!m.versions[b]"><span class="absent">—</span></template>
                    <template v-else-if="m.versions[b].status === 'available'">
                      <a
                        v-if="m.versions[b].readme_path"
                        href="#"
                        :title="`README on ${b}`"
                        @click.prevent="navigate(r.repository_id, m.versions[b].readme_path, b)"
                      >{{ m.versions[b].version || 'no version' }}</a>
                      <span v-else>{{ m.versions[b].version || 'no version' }}</span>
                      <span v-if="m.versions[b].manifest_error" class="eta">{{ m.versions[b].manifest_error }}</span>
                    </template>
                    <template v-else>
                      <span class="pill" :class="m.versions[b].status">{{ m.versions[b].status }}</span>
                      <span v-if="m.versions[b].eta" class="eta">eta {{ m.versions[b].eta }}</span>
                      <span v-else-if="m.versions[b].note" class="eta">{{ m.versions[b].note }}</span>
                    </template>
                  </td>
                  <td class="toggle">
                    <button
                      class="feat"
                      type="button"
                      :aria-expanded="isOpen(r, m)"
                      :aria-label="isOpen(r, m) ? 'Hide details' : 'Show details'"
                      @click="toggle(r, m)"
                    >{{ isOpen(r, m) ? '−' : '+' }}</button>
                  </td>
                </tr>
                <tr v-if="isOpen(r, m)" class="features">
                  <td :colspan="5 + details[r.repository_id].data.branches.length">
                    <div class="label">Features</div>
                    <ul v-if="m.features.length"><li v-for="f in m.features" :key="f">{{ f }}</li></ul>
                    <p v-else class="muted">No features listed in product.yml.</p>
                    <div class="readme" v-if="readmes[key(r, m)]">
                      <div class="label">
                        README<template v-if="readmes[key(r, m)].branch"> · {{ readmes[key(r, m)].branch }}</template>
                        <a
                          v-if="readmes[key(r, m)].path"
                          href="#"
                          @click.prevent="navigate(r.repository_id, readmes[key(r, m)].path, readmes[key(r, m)].branch)"
                        >open in docs ↗</a>
                      </div>
                      <p v-if="readmes[key(r, m)].none" class="muted">No README on any version branch.</p>
                      <p v-else-if="readmes[key(r, m)].loading" class="muted">Loading README…</p>
                      <p v-else-if="readmes[key(r, m)].error" class="error">{{ readmes[key(r, m)].error }}</p>
                      <!-- html is DOMPurify-sanitized in renderMarkdown before it reaches v-html -->
                      <div v-else class="markdown-body" v-html="readmes[key(r, m)].html" @click="onReadmeClick($event, r, m)"></div>
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>

        <div class="future">
          <h3>Open issues</h3>
          <p v-if="!details[r.repository_id].data.issues.length" class="empty">No open issues.</p>
          <ul v-else>
            <li
              v-for="i in details[r.repository_id].data.issues"
              :key="i.number"
              :class="i.state"
            >
              <span class="num">#{{ i.number }}</span>
              <span class="chip" :class="i.state">{{ stateLabel[i.state] }}</span>
              <a :href="i.url" target="_blank" rel="noopener noreferrer">{{ i.title }}</a>
              <span v-if="i.milestone" class="ms">{{ i.milestone }}</span>
              <span class="age">
                <template v-if="i.state === 'wont_do'">closed {{ age(i.closed_at) }}</template>
                <template v-else-if="i.pr_number">PR #{{ i.pr_number }} open</template>
                <template v-else-if="i.assignee">assigned to {{ i.assignee }}</template>
                <template v-else>{{ age(i.created_at) }}</template>
              </span>
            </li>
          </ul>
        </div>
      </template>
    </section>
  </article>
</template>
