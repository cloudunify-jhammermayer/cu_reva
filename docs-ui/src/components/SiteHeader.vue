<script setup>
import { route, goPage } from '../location.js'

// The header every consultant page shares (see docs-ui/src/reva.css). Status,
// How it works and Backups are static pages the api serves under /reviews/;
// Docs and Internal modules are this SPA's two pages.
const subtitle = () => (route.value.page === 'internal-modules' ? '/ internal modules' : '/ docs')
</script>

<template>
  <header class="site-header">
    <h1>REVA <span>{{ subtitle() }}</span></h1>
    <nav class="site-nav">
      <a href="./" :class="{ active: !route.page }" @click.prevent="goPage()">Docs</a>
      <a href="/reviews/">Status</a>
      <a href="/reviews/how-it-works">How it works</a>
      <a
        href="?page=internal-modules"
        :class="{ active: route.page === 'internal-modules' }"
        @click.prevent="goPage('internal-modules')"
        >Internal modules</a
      >
      <a href="/reviews/backups">Backups</a>
    </nav>
  </header>
</template>
