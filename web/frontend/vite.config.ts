import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8088',
        changeOrigin: true,
      },
    },
  },
  // `preview` serves the production build the way the deployed image does, and
  // proxies `/api` so the built bundle can be audited against the real core
  // (scripts/bench/ui_live_audit.mjs) instead of a mock.
  preview: {
    port: 4173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8080',
        changeOrigin: true,
      },
    },
  },
  build: {
    // Vendor code changes far less often than dashboard code: splitting it keeps
    // `react` in the browser cache across UI deploys, and keeps every chunk well
    // inside the 200 KB gzip cap in docs/ui-ux-system.md §5.
    rollupOptions: {
      output: {
        // Rolldown (Vite 8) takes a function, not the object map Rollup accepted.
        manualChunks(id: string) {
          if (/node_modules\/(react|react-dom|scheduler)\//.test(id)) return 'react'
          if (id.includes('node_modules/lucide-react')) return 'icons'
          return undefined
        },
      },
    },
  },
})
