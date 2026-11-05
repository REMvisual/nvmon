import { test, expect } from '@playwright/test';

// Smoke tests against the nvmon controller dashboard on :9997.
// Run prerequisite: start the controller (NVMON/run_monitor.bat or
// `python\python.exe -m nvmon controller`). If a controller password is set,
// export NVMON_PASSWORD=<pw> before running tests; the config picks it up
// as the X-NVMON-Password header.

test('controller serves a page on /', async ({ page }) => {
  const response = await page.goto('/');
  // Either the dashboard (200) or the login form (200 with no auth header) —
  // both are healthy. A 5xx means the controller is broken.
  expect(response, 'controller did not respond at /').not.toBeNull();
  expect(response!.status(), 'controller returned 5xx').toBeLessThan(500);
  await expect(page).toHaveTitle(/.+/);
});

test('/health endpoint returns JSON', async ({ request }) => {
  const res = await request.get('/health');
  expect(res.status(), '/health should be 200').toBe(200);
  const body = await res.json();
  expect(body, '/health body should be an object').toBeTruthy();
});

test('no console errors on landing page', async ({ page }) => {
  const errors: string[] = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text());
  });
  await page.goto('/');
  // Not 'networkidle': the dashboard holds an SSE connection to /stream
  // open for its whole life, so the network never goes idle and the wait
  // times out without ever checking the console. Wait for load + one
  // poll cycle instead.
  await page.waitForLoadState('load');
  await page.waitForTimeout(6_000);
  expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
});
