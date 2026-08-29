import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Base path for the deployed site. Defaults to /gavel/ (GitHub Pages project
// site for the template repo); CI overrides via BASE_PATH for instances
// deployed under a different repo name or a custom domain ("/").
export default defineConfig({
  plugins: [react()],
  base: process.env.BASE_PATH || '/gavel/',
  define: {
    // Stamped into the footer so data staleness is visible at a glance —
    // CI rebuilds on every pipeline run, so build time ≈ data freshness.
    __BUILD_DATE__: JSON.stringify(new Date().toISOString()),
  },
  build: {
    outDir: 'dist',
    rollupOptions: {
      input: 'index.html',
    },
  },
})
