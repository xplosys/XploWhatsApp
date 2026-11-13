import { defineConfig } from "@playwright/test";

// The stack must already be running (see README.md): this suite never starts or stops it.
export default defineConfig({
  testDir: "./tests",
  globalSetup: "./tests/platform-setup.ts",
  timeout: 180_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  outputDir: "test-results",
  use: {
    baseURL: process.env.E2E_WEB_URL ?? "http://127.0.0.1:3000",
    viewport: { width: 1366, height: 900 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
});
