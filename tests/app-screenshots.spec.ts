import { test } from '@playwright/test';
import * as path from 'path';
import * as fs from 'fs';

// Screenshots of the live nvmon dashboard. Assumes the controller is
// running at http://localhost:9997 (start with NVMON\run_monitor.bat or
// `python\python.exe -m nvmon controller`).

const repoRoot = path.resolve(__dirname, '..');
const shotsDir = path.join(repoRoot, 'tests/screenshots');
fs.mkdirSync(shotsDir, { recursive: true });

const BASE = process.env.NVMON_CONTROLLER_URL || 'http://localhost:9997';
const PASSWORD = process.env.NVMON_PASSWORD || 'Pass';

test.use({
  viewport: { width: 1600, height: 1000 },
  extraHTTPHeaders: { 'X-NVMON-Password': PASSWORD },
});

test('login page', async ({ page }) => {
  // Hit the login form directly (no header auth) so it actually renders
  const ctx = await page.context();
  await ctx.clearCookies();
  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(400);
  await page.screenshot({
    path: path.join(shotsDir, 'dashboard-login.png'),
    fullPage: false,
  });
});

test('dashboard home', async ({ page }) => {
  // Authenticate via the header (set in test.use above), then load the dashboard
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500); // let alpine/htmx mount, first aggregator tick, charts paint
  await page.screenshot({
    path: path.join(shotsDir, 'dashboard-home.png'),
    fullPage: false,
  });
});

test('dashboard full page', async ({ page }) => {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);
  await page.screenshot({
    path: path.join(shotsDir, 'dashboard-full.png'),
    fullPage: true,
  });
});

test('node detail slide-over (first card)', async ({ page }) => {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);
  // First node card — click to open the slide-over detail
  const firstCard = page.locator('[data-node-card], .node-card, .card').first();
  if (await firstCard.count()) {
    await firstCard.click({ trial: false }).catch(() => {});
    await page.waitForTimeout(600);
    await page.screenshot({
      path: path.join(shotsDir, 'dashboard-node-detail.png'),
      fullPage: false,
    });
  } else {
    test.skip(true, 'no node cards present to click');
  }
});
