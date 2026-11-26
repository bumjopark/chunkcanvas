import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// In development the API runs separately; set CC_API_URL if it is not on port 8300.
const api = process.env.CC_API_URL ?? 'http://127.0.0.1:8300';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, proxy: { '/api': { target: api, changeOrigin: false } } },
  build: { target: 'es2022', chunkSizeWarningLimit: 2500 },
});
