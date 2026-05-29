import type { NextConfig } from "next";

// Proxy /api/* and /ws/* on the Next.js dev server to the FastAPI backend.
// Routing both through the same origin as the page means port-forwarding
// 8088 alone covers HTTP + WebSocket — no separate forward for :8503 needed.
// Set BOARDROOM_API_URL to point at a side instance (e.g. http://localhost:8504
// for the demo). Defaults to the live API on :8503.
const API_URL = process.env.BOARDROOM_API_URL || "http://localhost:8503";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${API_URL}/:path*`,
      },
      {
        // WebSocket upgrade requests pass through Next.js rewrites.
        source: "/ws/:path*",
        destination: `${API_URL}/ws/:path*`,
      },
    ];
  },
};

export default nextConfig;
