import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The backend's CORS settings allow http://localhost:5173 and
// http://127.0.0.1:5173, so the dev server must stay on port 5173.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, strictPort: true },
});
