import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

// Prepares the business a load test runs against (load/prepare.spec.ts).
export default defineConfig({ ...base, testDir: "./load", testMatch: /prepare\.spec\.ts/ });
