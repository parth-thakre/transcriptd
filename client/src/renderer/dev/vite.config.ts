// Renderer-only preview with a mocked desktop bridge. Not used by electron-vite.
// Run from client/: bunx vite --config src/renderer/dev/vite.config.ts
import { resolve } from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const root = resolve(import.meta.dirname, '..')
export default defineConfig({
  root,
  plugins: [react(), {
    name: 'transcriptd-dev-mock',
    apply: 'serve',
    transformIndexHtml: html => html
      .replace("script-src 'self'", "script-src 'self' 'unsafe-inline'")
      .replace('ws://localhost:*', 'ws: wss:')
      .replace('<script type="module"', '<script type="module" src="/dev/mock.ts"></script><script type="module"')
  }],
  // Binds all interfaces so a remote preview browser can reach it. Stop it when done.
  server: { host: true, allowedHosts: true, port: Number(process.env.PORT ?? 5199), strictPort: true }
})
