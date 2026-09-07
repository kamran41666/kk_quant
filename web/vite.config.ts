import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  server: {
    port: 5173,
    proxy: {
      // The API binds loopback explicitly; use the same address for both
      // HTTP and WebSocket so Windows localhost/IPv6 resolution cannot break
      // the realtime paper-portfolio channel.
      '/api': 'http://127.0.0.1:8000',
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    }
  },
  build: {
    modulePreload: {
      // ECharts is only required by chart-bearing routes.  Do not fetch its
      // large core bundle while the dashboard shell is still loading.
      resolveDependencies(_filename, dependencies) {
        return dependencies.filter((dependency) => !dependency.includes('charts-core'))
      },
    },
    rollupOptions: {
      output: {
        // Keep charting and application runtimes out of the initial install
        // chunk so the research pages can load progressively.
        manualChunks(id) {
          if (id.includes('/node_modules/echarts/')) {
            if (id.includes('/charts/')) return 'charts-series'
            if (id.includes('/components/')) return 'charts-components'
            return 'charts-core'
          }
          if (id.includes('/node_modules/vue-echarts')) return 'charts-adapter'
          if (
            id.includes('/node_modules/vue/')
            || id.includes('/node_modules/vue-router/')
            || id.includes('/node_modules/pinia/')
          ) return 'framework'
          return undefined
        },
      },
    },
  }
})
