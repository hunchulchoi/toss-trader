const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests',
  testMatch: '**/*.pw.js',
  outputDir: 'test-results/playwright',
  timeout: 30_000,
  use: {
    baseURL: 'http://127.0.0.1:18091',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'PYTHONPATH=src python3 tests/serve_timeline_fixture.py',
    url: 'http://127.0.0.1:18091/healthz',
    reuseExistingServer: false,
    timeout: 15_000,
  },
});
