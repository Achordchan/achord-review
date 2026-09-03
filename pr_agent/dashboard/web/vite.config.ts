import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

// Build output lands in ../static so pr_agent/servers/github_app.py serves it
// without a Node runtime. The absolute /dashboard/ base matters: assets are
// served from the /dashboard/assets mount, and a relative base would resolve
// against the current route (e.g. /dashboard/reviews/assets/...) and 404 the
// SPA on any deep link.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: '/dashboard/',
  build: {
    outDir: '../static',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500,
  },
  server: {
    // dev only: proxy API + SPA mount to a locally running github_app
    proxy: {
      '/api': 'http://127.0.0.1:33001',
    },
  },
})
