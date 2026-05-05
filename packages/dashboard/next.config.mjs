// next.config — Next.js 14 (config.ts isn't supported until 15)
// API proxy: dashboard talks to /api/* → forwarded to the FastAPI backend.
// Same-origin in the browser → no CORS to add to the API.
const API_URL = process.env.LUMIN_API_URL || 'http://localhost:8000';

/** @type {import('next').NextConfig} */
const config = {
  // Standalone mode: produces .next/standalone with a self-contained
  // Node server, used by the Docker image.
  output: 'standalone',
  async redirects() {
    // Config-level redirect emits a proper HTTP Location header for all
    // clients. (A `redirect()` call inside a Server Component returns
    // 307 with no Location, only an RSC body — fine for browsers, broken
    // for curl / health probes / non-JS clients.)
    return [
      {
        source: '/',
        destination: '/agents',
        permanent: false,
      },
    ];
  },
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${API_URL}/:path*`,
      },
      // FastAPI's auto-generated /docs page hardcodes
      // `url: '/openapi.json'` (browser-relative). Loading it through
      // the dashboard proxy at /api/docs means the browser then tries
      // to fetch /openapi.json from localhost:3000 — which 404s. Proxy
      // that one absolute path too so Swagger UI can pull the spec.
      {
        source: '/openapi.json',
        destination: `${API_URL}/openapi.json`,
      },
    ];
  },
};

export default config;
