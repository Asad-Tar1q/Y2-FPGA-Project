import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,   // listen on 0.0.0.0 so you can open it from a tablet on the same WiFi
    port: 5173,
  },
});
