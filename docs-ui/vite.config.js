import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// In dev, Vite serves the SPA and proxies the docs API to the local api
// container (`make dev` → api on :8080). In prod the built `dist/` is served
// from the same origin as the api (behind Cloudflare Access), so the relative
// `/repo-docs` calls resolve without a proxy.
export default defineConfig({
  // Served under /docs/ by the prod nginx (and the local demo). Assets resolve
  // to /docs/assets/*; the SPA's /repo-docs API calls are absolute, unaffected.
  base: '/docs/',
  plugins: [vue()],
  server: {
    proxy: {
      '/repo-docs': {
        target: process.env.REVA_API_URL || 'http://localhost:8080',
        changeOrigin: true,
      },
    },
  },
})
