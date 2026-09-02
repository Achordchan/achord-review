import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

// Build output lands in ../static so pr_agent/servers/github_app.py serves it
// without a Node runtime. Relative base keeps the SPA working under /dashboard/.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: './',
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
