import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Base path for the deployed site. Defaults to /gavel/ (GitHub Pages project
// site for the template repo); CI overrides via BASE_PATH for instances
// deployed under a different repo name or a custom domain ("/").
export default defineConfig({
  plugins: [react()],
  base: process.env.BASE_PATH || '/gavel/',
  build: {
    outDir: 'dist',
    rollupOptions: {
      input: 'index.html',
    },
  },
})
