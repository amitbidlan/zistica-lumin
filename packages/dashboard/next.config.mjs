// next.config — Next.js 14 (config.ts isn't supported until 15)
// API proxy: dashboard talks to /api/* → forwarded to the FastAPI backend.
// Same-origin in the browser → no CORS to add to the API.
const API_URL = process.env.SYNAPTIC_API_URL || 'http://localhost:8000';

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
        destination: '/traces',
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
    ];
  },
};

export default config;
