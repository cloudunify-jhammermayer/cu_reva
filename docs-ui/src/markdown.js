// Markdown or HTML -> { html (sanitized), toc, hasMermaid }.
//
// - Repo-relative links/images are rewritten so docs render outside their repo
//   (images -> /raw proxy; *.md links -> in-app nav; other -> GitHub blob).
// - Headings get GitHub-style slug ids + a click-to-anchor, and feed the TOC.
// - ```mermaid blocks become <div class="mermaid"> for DocView to render lazily.
// DOMPurify runs before any rewrite, so doc content can never inject script.
// <style> is stripped from both markdown- and html-sourced docs.

import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/common'
import { rawUrl } from './api.js'

const md = new MarkdownIt({
  html: true,
  linkify: true,
  highlight(code, lang) {
    // Leave mermaid for DocView; highlight known languages, escape the rest.
    if (lang && lang !== 'mermaid' && hljs.getLanguage(lang)) {
      try {
        return hljs.highlight(code, { language: lang }).value
      } catch { /* fall through */ }
    }
    return ''
  },
})

const EXTERNAL = /^([a-z][a-z0-9+.-]*:|\/\/|#)/i

// A doc's <style> block emits GLOBAL css into the SPA's own page and can
// restyle or overlay it; inline style="…" cannot escape its element, so it
// stays — it is what keeps an exported table looking like a table.
const SANITIZE = { USE_PROFILES: { html: true }, FORBID_TAGS: ['style'] }

function dirname(p) {
  const i = p.lastIndexOf('/')
  return i === -1 ? '' : p.slice(0, i)
}

function resolvePath(baseDir, rel) {
  const stack = baseDir ? baseDir.split('/') : []
  for (const part of rel.split('/')) {
    if (part === '' || part === '.') continue
    if (part === '..') stack.pop()
    else stack.push(part)
  }
  return stack.join('/')
}

// GitHub-style heading slug, deduped with -1/-2 suffixes.
function slugify(text, seen) {
  const base = text.toLowerCase().trim().replace(/[^\w\s-]/g, '').replace(/\s+/g, '-') || 'section'
  let slug = base
  let n = 1
  while (seen.has(slug)) slug = `${base}-${n++}`
  seen.add(slug)
  return slug
}

// Shared post-sanitize pipeline: rewrite relative images/links, add heading
// anchors + TOC, extract mermaid. Operates on the sanitized DOM, so it is
// identical for markdown-sourced and html-sourced docs.
function postProcess(clean, { repoId, path, owner, name, branch }) {
  const baseDir = dirname(path)
  const tpl = document.createElement('template')
  tpl.innerHTML = clean

  // Relative images -> /raw proxy.
  for (const img of tpl.content.querySelectorAll('img[src]')) {
    const src = img.getAttribute('src')
    if (src && !EXTERNAL.test(src)) {
      img.setAttribute('src', rawUrl(repoId, resolvePath(baseDir, src)))
      img.setAttribute('loading', 'lazy')
    }
  }

  // Links: in-repo .md -> in-app nav; other repo files -> GitHub; external -> new tab.
  for (const a of tpl.content.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href')
    if (!href) continue
    if (EXTERNAL.test(href)) {
      if (!href.startsWith('#')) {
        a.setAttribute('target', '_blank')
        a.setAttribute('rel', 'noopener noreferrer')
      }
      continue
    }
    const [relPath, anchor] = href.split('#')
    const resolved = resolvePath(baseDir, relPath)
    if (/\.(md|markdown|html?)$/i.test(resolved)) {
      const r = branch ? `&ref=${encodeURIComponent(branch)}` : ''
      a.setAttribute('href', `?repo=${repoId}&path=${encodeURIComponent(resolved)}${r}${anchor ? '#' + anchor : ''}`)
      a.setAttribute('data-doc-path', resolved)
      if (anchor) a.setAttribute('data-doc-anchor', anchor)
    } else if (owner && name) {
      a.setAttribute('href', `https://github.com/${owner}/${name}/blob/${branch}/${resolved}`)
      a.setAttribute('target', '_blank')
      a.setAttribute('rel', 'noopener noreferrer')
    }
  }

  // Heading anchors + table of contents (h2/h3).
  const seen = new Set()
  const toc = []
  for (const h of tpl.content.querySelectorAll('h1, h2, h3, h4')) {
    const id = slugify(h.textContent || '', seen)
    h.id = id
    const link = document.createElement('a')
    link.className = 'heading-anchor'
    link.setAttribute('href', `#${id}`)
    link.setAttribute('aria-label', 'Link to this section')
    link.textContent = '#'
    h.prepend(link)
    const level = Number(h.tagName[1])
    if (level === 2 || level === 3) toc.push({ level, text: (h.textContent || '').replace(/^#/, ''), id })
  }

  // Fenced ```mermaid -> <div class="mermaid"> (rendered lazily in DocView).
  let hasMermaid = false
  for (const code of tpl.content.querySelectorAll('pre > code.language-mermaid')) {
    const div = document.createElement('div')
    div.className = 'mermaid'
    div.textContent = code.textContent || ''
    code.parentElement.replaceWith(div)
    hasMermaid = true
  }

  return { html: tpl.innerHTML, toc, hasMermaid }
}

export function renderMarkdown(markdown, ctx) {
  return postProcess(DOMPurify.sanitize(md.render(markdown || ''), SANITIZE), ctx)
}

// An HTML doc is the same pipeline minus markdown-it. DOMPurify's html profile
// drops <script>, event handlers and javascript: URLs, and flattens a
// standalone document's <html>/<head>/<body> wrapper — so the doc body renders
// inside .markdown-body and inherits site typography, which is the intent.
export function renderHtml(source, ctx) {
  return postProcess(DOMPurify.sanitize(source || '', SANITIZE), ctx)
}
