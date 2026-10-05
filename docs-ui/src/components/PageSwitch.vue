<script setup>
import { route, goPage } from '../location.js'

// The three consultant-facing pages. "How it works" is the static page the api
// serves next to the review status page, so it is a plain link out of the SPA;
// its own nav links back here.
</script>

<template>
  <nav class="pages">
    <a href="./" :class="{ active: !route.page }" @click.prevent="goPage()">Docs</a>
    <a href="/reviews/how-it-works">How it works</a>
    <a
      href="?page=internal-modules"
      :class="{ active: route.page === 'internal-modules' }"
      @click.prevent="goPage('internal-modules')"
      >Internal modules</a
    >
  </nav>
</template>
