import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5000,
    host: '0.0.0.0',
    proxy: {
      '/api': 'http://localhost:8000',
      // /health is outside the /api prefix but still served by the backend;
      // without this the start screen's key check would 404 against Vite.
      '/health': 'http://localhost:8000'
    }
  }
})
