import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // SSE 必须关闭代理缓冲，否则前端收不到增量
      '/api': {
        target: 'http://localhost:8080',
        changeOrigin: true,
      },
    },
  },
})