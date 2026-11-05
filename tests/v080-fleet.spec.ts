import { test, expect } from '@playwright/test';

/**
 * v0.8.0 fleet-hardening UI checks.
 *
 * Prereqs — three processes, all from the workspace root, each self-stops:
 *   cd NVMON && python\python.exe -m nvmon agent                       (real agent :9998)
 *   NVMON\python\python.exe -I tests\fixtures\fake_applying_agent.py 1200   (fake agent :19997)
 *   cd NVMON && python\python.exe -m nvmon controller --config ..\tests\fixtures\controller-playwright.toml
 * The fake agent reports an apply IN PROGRESS and answers POST /apply/cancel
 * with {"cancelling": true}; the toml lists both as [controller].node_ips
 * and runs the controller without a password.
 *
 * Set NVMON_FAKE_HOST / NVMON_REAL_HOST to override the hostnames the
 * two agents report (defaults: FAKE-APPLYING and this machine's name).
 */
const FAKE = process.env.NVMON_FAKE_HOST || 'FAKE-APPLYING';
const REAL = process.env.NVMON_REAL_HOST || (process.env.COMPUTERNAME || '').toUpperCase();

async function waitForNode(page: any, host: string) {
  // The IP-list entry is renamed to the agent's hostname on the first
  // successful poll; the page reloads itself on node_appeared. Poll /nodes
  // until the real name is there, then load the dashboard.
  await expect.poll(async () => {
    const r = await page.request.get('/nodes');
    if (!r.ok()) return '';
    const j = await r.json();
    return (j.nodes || []).map((n: any) => n.hostname).join(',');
  }, { timeout: 30_000 }).toContain(host);
}

test('IP-list nodes resolve to hostnames and render as cards', async ({ page }) => {
  const errors: string[] = [];
  page.on('console', (msg) => { if (msg.type() === 'error') errors.push(msg.text()); });
  await waitForNode(page, FAKE);
  if (REAL) await waitForNode(page, REAL);
  await page.goto('/');
  await expect(page.locator(`.node[data-hostname="${FAKE}"]`)).toBeVisible();
  if (REAL) await expect(page.locator(`.node[data-hostname="${REAL}"]`)).toBeVisible();
  // No card may still be labelled with the raw IP placeholder.
  await expect(page.locator('.node[data-hostname="127.0.0.1:9998"]')).toHaveCount(0);
  await expect(page.locator('.node[data-hostname="127.0.0.1:19997"]')).toHaveCount(0);
  // Open both slide-overs too, then assert the console stayed clean.
  await page.locator(`.node[data-hostname="${FAKE}"]`).click();
  await expect(page.locator(`#job-banner-${FAKE}`)).toBeVisible({ timeout: 10_000 });
  await page.keyboard.press('Escape');
  if (REAL) {
    await page.locator(`.node[data-hostname="${REAL}"]`).click();
    await expect(page.locator(`#slideover-host-${REAL}`)).toBeVisible({ timeout: 10_000 });
  }
  await page.waitForTimeout(4_000);
  expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
});

test('tile shows the applying marker for a node whose agent is mid-apply', async ({ page }) => {
  await waitForNode(page, FAKE);
  await page.goto('/');
  const marker = page.locator(`.node[data-hostname="${FAKE}"] [data-field="status_age"]`);
  await expect(marker).toBeVisible();
  await expect(marker).toContainText('applying');
  // Real agent: idle and fresh -> marker hidden.
  if (REAL) {
    const realMarker = page.locator(`.node[data-hostname="${REAL}"] [data-field="status_age"]`);
    await expect(realMarker).toBeHidden();
  }
});

test('SSE keeps the applying marker alive after a live node_update', async ({ page }) => {
  await waitForNode(page, FAKE);
  await page.goto('/');
  const marker = page.locator(`.node[data-hostname="${FAKE}"] [data-field="status_age"]`);
  await expect(marker).toContainText('applying');
  // Wait past one poll + SSE tick; the JS handler must not hide it.
  await page.waitForTimeout(7_000);
  await expect(marker).toBeVisible();
  await expect(marker).toContainText('applying');
});

test('slide-over shows the job banner with step and a working Cancel button', async ({ page }) => {
  await waitForNode(page, FAKE);
  await page.goto('/');
  await page.locator(`.node[data-hostname="${FAKE}"]`).click();
  const banner = page.locator(`#job-banner-${FAKE}`);
  await expect(banner).toBeVisible({ timeout: 10_000 });
  await expect(banner).toContainText('Applying');
  await expect(banner).toContainText('24_Server');
  await expect(banner).toContainText('[4/6] Configuring Mosaic');
  await expect(banner).toContainText('snapshot 47s old');

  // output_signal validation row is rendered and red.
  const portal = page.locator('#slideover-portal');
  await expect(portal).toContainText('output_signal');
  await expect(portal).toContainText('sent as 10240x5760@60Hz (scaled)');

  // Cancel -> POST /node/<h>/apply/cancel -> fake agent says cancelling -> toast.
  const cancelReq = page.waitForRequest((r) => r.url().includes(`/node/${FAKE}/apply/cancel`) && r.method() === 'POST');
  await banner.getByRole('button', { name: 'Cancel' }).click();
  await cancelReq;
  const toast = page.locator('.nvmon-toast');
  await expect(toast.first()).toBeVisible({ timeout: 5_000 });
  await expect(toast.first()).toContainText(`CANCEL sent to ${FAKE}`);
});

test('real agent: slide-over has no job banner, no STALE, output_signal validates', async ({ page }) => {
  test.skip(!REAL, 'no real agent hostname');
  await waitForNode(page, REAL);
  await page.goto('/');
  await page.locator(`.node[data-hostname="${REAL}"]`).click();
  const portal = page.locator('#slideover-portal');
  await expect(portal.locator(`#slideover-host-${REAL}`)).toBeVisible({ timeout: 10_000 });
  await expect(portal.locator(`#job-banner-${REAL}`)).toHaveCount(0);
  await expect(portal).not.toContainText('STALE');
  // The CCD-based output_signal check is rendered for the real agent. Its
  // verdict depends on the auto-selected profile (default.json expects 24 Hz
  // on a 60 Hz dev monitor), so only assert the row exists.
  await expect(portal).toContainText('output_signal');
});

test('controller passthroughs: mosaic timings, displays signals, apply cancel', async ({ page }) => {
  await waitForNode(page, FAKE);
  if (REAL) {
    const t = await page.request.get(`/node/${REAL}/mosaic/timings?rows=1&cols=1&res=1920,1080,60`);
    expect(t.status()).toBe(200);
    const tj = await t.json();
    expect(tj).toHaveProperty('command');
    expect(tj).toHaveProperty('rate_hz');
    const s = await page.request.get(`/node/${REAL}/displays/signals`);
    expect(s.status()).toBe(200);
    expect(Array.isArray((await s.json()).signals)).toBeTruthy();
    // Real agent is not elevated here -> the fan-out tally reports 403.
    const c = await page.request.post(`/node/${REAL}/apply/cancel`, { data: {} });
    expect(c.status()).toBe(200);
    const cj = await c.json();
    expect(cj.results[0].hostname).toBe(REAL);
    expect(cj.results[0].http_code).toBe(403);
  }
  const fc = await page.request.post(`/node/${FAKE}/apply/cancel`, { data: {} });
  expect(fc.status()).toBe(200);
  const fcj = await fc.json();
  expect(fcj.results[0].http_code).toBe(200);
  expect(fcj.results[0].body.cancelling).toBe(true);
});

test('fanout job apply returns a preflight report', async ({ page }) => {
  await waitForNode(page, FAKE);
  const r = await page.request.post('/fanout/job', {
    data: { action: 'apply', profile: 'does-not-exist-profile', scope: [FAKE] },
  });
  expect(r.status()).toBe(200);
  const j = await r.json();
  expect(j).toHaveProperty('preflight');
  expect(Array.isArray(j.preflight)).toBeTruthy();
});
