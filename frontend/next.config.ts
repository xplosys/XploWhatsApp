import type { NextConfig } from "next";

// The browser always calls same-origin /api/*. In development Next proxies it to
// the API; in production Nginx routes app.xplowhatsapp.com/api to the API service.
const apiOrigin = process.env.API_INTERNAL_URL ?? "http://localhost:8000";

const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];

// The /api proxy buffers request bodies and cuts them at 10 MB by default, which would
// break knowledge uploads; allow the API's own limit plus room for the form encoding.
const uploadLimitMb = Number(process.env.KNOWLEDGE_MAX_FILE_MB ?? 20) + 2;

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  experimental: { proxyClientMaxBodySize: `${uploadLimitMb}mb` },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
  },
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
