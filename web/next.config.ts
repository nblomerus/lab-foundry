import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Proxy /api/* on the Next.js dev server to the FastAPI backend.
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://localhost:8503/:path*",
      },
    ];
  },
};

export default nextConfig;
