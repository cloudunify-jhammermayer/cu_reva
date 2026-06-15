// Markdown -> sanitized HTML, with repo-relative links/images rewritten so docs
// render correctly outside their repo:
//   - relative <img> -> the /raw proxy (private repos can't use github raw URLs)
//   - relative *.md links -> in-app navigation (data-doc-path, handled in DocView)
//   - relative non-doc links -> GitHub blob URL in a new tab
//   - external/anchor links left intact (anchors scroll natively under our
//     query-param router)
// DOMPurify runs before any rewrite, so doc content can never inject script.

import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/common'
import { rawUrl } from './api.js'

const md = new MarkdownIt({
  html: true,
  linkify: true,
  highlight(code, lang) {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return hljs.highlight(code, { language: lang }).value
      } catch { /* fall through to default escaping */ }
    }
    return ''
  },
})

// http:, https:, mailto:, data:, protocol-relative //, or in-page #anchor.
const EXTERNAL = /^([a-z][a-z0-9+.-]*:|\/\/|#)/i

function dirname(p) {
  const i = p.lastIndexOf('/')
  return i === -1 ? '' : p.slice(0, i)
}

// Resolve a relative ref against the directory of the current doc.
function resolvePath(baseDir, rel) {
  const stack = baseDir ? baseDir.split('/') : []
  for (const part of rel.split('/')) {
    if (part === '' || part === '.') continue
    if (part === '..') stack.pop()
    else stack.push(part)
  }
  return stack.join('/')
}

export function renderMarkdown(markdown, { repoId, path, owner, name, branch }) {
  const baseDir = dirname(path)
  const clean = DOMPurify.sanitize(md.render(markdown || ''), { USE_PROFILES: { html: true } })

  const tpl = document.createElement('template')
  tpl.innerHTML = clean

  for (const img of tpl.content.querySelectorAll('img[src]')) {
    const src = img.getAttribute('src')
    if (src && !EXTERNAL.test(src)) {
      img.setAttribute('src', rawUrl(repoId, resolvePath(baseDir, src)))
      img.setAttribute('loading', 'lazy')
    }
  }

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
    if (/\.(md|markdown)$/i.test(resolved)) {
      const r = branch ? `&ref=${encodeURIComponent(branch)}` : ''
      a.setAttribute('href', `?repo=${repoId}&path=${encodeURIComponent(resolved)}${r}${anchor ? '#' + anchor : ''}`)
      a.setAttribute('data-doc-path', resolved)
    } else if (owner && name) {
      a.setAttribute('href', `https://github.com/${owner}/${name}/blob/${branch}/${resolved}`)
      a.setAttribute('target', '_blank')
      a.setAttribute('rel', 'noopener noreferrer')
    }
  }

  return tpl.innerHTML
}
