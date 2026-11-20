import { resolve } from 'node:path'
import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  main: { plugins: [externalizeDepsPlugin()] },
  preload: {
    plugins: [externalizeDepsPlugin()],
    build: { rollupOptions: { output: { format: 'cjs', entryFileNames: 'index.cjs' } } }
  },
  renderer: {
    plugins: [react(), {
      name: 'desktop-dev-csp',
      apply: 'serve',
      // React Refresh injects an inline preamble in development only.
      // Production HTML retains script-src 'self' without unsafe-inline.
      transformIndexHtml: html => html
        .replace("script-src 'self'", "script-src 'self' 'unsafe-inline'")
        .replace('ws://localhost:*', 'ws://localhost:* ws://127.0.0.1:*')
    }],
    build: {
      minify: 'esbuild',
      rollupOptions: {
        input: {
          main: resolve('src/renderer/index.html'),
          overlay: resolve('src/renderer/overlay.html'),
          vocab: resolve('src/renderer/vocab.html')
        }
      }
    }
  }
})
