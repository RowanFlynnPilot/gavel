import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The app sets its title, fonts and theme at runtime from instance.json, but
// link previews (Open Graph), favicons and search snippets are read by
// crawlers that don't run JavaScript. This writes those tags into index.html
// at build time, from the same instance.json, so index.html itself stays
// free of newsroom strings.
//
// instance.json fields read (all optional): instance.name, instance.newsroom,
// theme.primary_dark, seo.pages_base_url, seo.noindex, seo.description,
// seo.og_image, seo.favicon, seo.touch_icon (paths relative to the site root,
// e.g. "assets/og-image.jpg").
function instanceHead() {
  return {
    name: 'gavel-instance-head',
    transformIndexHtml(html) {
      const file = ['../instance.json', './public/instance.json']
        .map((p) => fileURLToPath(new URL(p, import.meta.url)))
        .find((p) => existsSync(p))
      if (!file) return html
      const cfg = JSON.parse(readFileSync(file, 'utf8'))
      const inst = cfg.instance || {}
      const seo = cfg.seo || {}
      const base = (seo.pages_base_url || '').replace(/\/+$/, '')
      const abs = (p) => (base ? `${base}/${p.replace(/^\/+/, '')}` : `./${p.replace(/^\/+/, '')}`)
      const title = [inst.name, inst.newsroom].filter(Boolean).join(' — ')
      const meta = (attrs) => ({ tag: 'meta', attrs, injectTo: 'head' })
      const link = (attrs) => ({ tag: 'link', attrs, injectTo: 'head' })
      const tags = []

      if (seo.noindex) tags.push(meta({ name: 'robots', content: 'noindex, nofollow' }))
      if (seo.description) tags.push(meta({ name: 'description', content: seo.description }))
      if (cfg.theme && cfg.theme.primary_dark) {
        tags.push(meta({ name: 'theme-color', content: cfg.theme.primary_dark }))
      }
      if (base && !seo.noindex) tags.push(link({ rel: 'canonical', href: `${base}/` }))
      if (seo.favicon) tags.push(link({ rel: 'icon', href: `./${seo.favicon}` }))
      if (seo.touch_icon) tags.push(link({ rel: 'apple-touch-icon', href: `./${seo.touch_icon}` }))

      if (inst.name) {
        tags.push(meta({ property: 'og:type', content: 'website' }))
        tags.push(meta({ property: 'og:title', content: inst.name }))
        tags.push(meta({ name: 'twitter:title', content: inst.name }))
        if (inst.newsroom) tags.push(meta({ property: 'og:site_name', content: inst.newsroom }))
        if (base) tags.push(meta({ property: 'og:url', content: `${base}/` }))
        if (seo.description) {
          tags.push(meta({ property: 'og:description', content: seo.description }))
          tags.push(meta({ name: 'twitter:description', content: seo.description }))
        }
        if (seo.og_image) {
          tags.push(meta({ property: 'og:image', content: abs(seo.og_image) }))
          tags.push(meta({ property: 'og:image:alt', content: title }))
          tags.push(meta({ name: 'twitter:card', content: 'summary_large_image' }))
          tags.push(meta({ name: 'twitter:image', content: abs(seo.og_image) }))
        }
      }

      const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
      return {
        html: title ? html.replace(/<title>[^<]*<\/title>/, `<title>${esc(title)}</title>`) : html,
        tags,
      }
    },
  }
}

// Base path for the deployed site. Defaults to /gavel/ (GitHub Pages project
// site for the template repo); CI overrides via BASE_PATH for instances
// deployed under a different repo name or a custom domain ("/").
export default defineConfig({
  plugins: [react(), instanceHead()],
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
