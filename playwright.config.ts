import { defineConfig, devices } from '@playwright/test';

// nvmon controller serves the unified dashboard on :9997.
// Tests assume the controller is already running (start it via NVMON/run_monitor.bat
// or `python\python.exe -m nvmon controller`). We don't auto-spawn it here because
// the controller binds firewalled ports and may require an admin install on a fresh
// box — keep that out-of-band.
export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'html',
  use: {
    baseURL: process.env.NVMON_CONTROLLER_URL || 'http://localhost:9997',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    extraHTTPHeaders: process.env.NVMON_PASSWORD
      ? { 'X-NVMON-Password': process.env.NVMON_PASSWORD }
      : undefined,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
