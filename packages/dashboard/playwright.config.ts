import { defineConfig, devices } from '@playwright/test';

/**
 * Browser-level E2E tests for the dashboard. Assumes the full stack is
 * running at http://localhost:3000 (dashboard) + http://localhost:8000
 * (API) — typically via the Docker container.
 *
 * Run locally:
 *   docker run -d --name e2e -p 3000:3000 -p 8000:8000 zistica/lumin:latest
 *   cd packages/dashboard && npm run test:e2e:install && npm run test:e2e
 */
export default defineConfig({
  testDir: './tests/e2e',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  // Tests share state via the API, run them serially to avoid races
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:3000',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
