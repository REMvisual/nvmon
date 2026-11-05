import { test, expect } from '@playwright/test';
import * as path from 'path';
import * as fs from 'fs';

// Screenshot helper: load an SVG file at a target render width and capture.
// We wrap the SVG in a minimal HTML page so the banner renders at the
// width the README would show on GitHub (~1000px content column) — that's
// the size readers will actually see.

const repoRoot = path.resolve(__dirname, '..');
const bannerDir = path.join(repoRoot, 'NVMON/nvmon/controller/dashboard_unified/static/img');
const shotsDir = path.join(repoRoot, 'tests/screenshots');

fs.mkdirSync(shotsDir, { recursive: true });

function wrapHtml(svgPath: string, bg: string) {
  const svg = fs.readFileSync(svgPath, 'utf-8');
  return `<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  html, body { margin: 0; padding: 0; background: ${bg}; }
  .container { max-width: 1000px; margin: 24px auto; padding: 24px; }
  .container svg { display: block; width: 100%; height: auto; }
</style>
</head><body>
<div class="container">${svg}</div>
</body></html>`;
}

test.describe('nvmon banner', () => {
  test('dark variant — github-width render', async ({ page }) => {
    const svgPath = path.join(bannerDir, 'nvmon-banner-dark.svg');
    await page.setViewportSize({ width: 1100, height: 700 });
    await page.setContent(wrapHtml(svgPath, '#0d1117'));  // github dark bg
    const container = page.locator('.container');
    await container.screenshot({
      path: path.join(shotsDir, 'banner-dark-github-width.png'),
    });
  });

  test('light variant — github-width render', async ({ page }) => {
    const svgPath = path.join(bannerDir, 'nvmon-banner-light.svg');
    await page.setViewportSize({ width: 1100, height: 700 });
    await page.setContent(wrapHtml(svgPath, '#ffffff'));  // github light bg
    const container = page.locator('.container');
    await container.screenshot({
      path: path.join(shotsDir, 'banner-light-github-width.png'),
    });
  });

  test('dark variant — full-resolution', async ({ page }) => {
    const svgPath = path.join(bannerDir, 'nvmon-banner-dark.svg');
    await page.setViewportSize({ width: 1700, height: 700 });
    await page.setContent(`<!DOCTYPE html><html><head>
<style>html,body{margin:0;padding:0;background:#0d1117;}
svg{display:block;}</style></head><body>
${fs.readFileSync(svgPath, 'utf-8')}
</body></html>`);
    const svg = page.locator('svg');
    await svg.screenshot({
      path: path.join(shotsDir, 'banner-dark-fullres.png'),
    });
  });

  test('light variant — full-resolution', async ({ page }) => {
    const svgPath = path.join(bannerDir, 'nvmon-banner-light.svg');
    await page.setViewportSize({ width: 1700, height: 700 });
    await page.setContent(`<!DOCTYPE html><html><head>
<style>html,body{margin:0;padding:0;background:#ffffff;}
svg{display:block;}</style></head><body>
${fs.readFileSync(svgPath, 'utf-8')}
</body></html>`);
    const svg = page.locator('svg');
    await svg.screenshot({
      path: path.join(shotsDir, 'banner-light-fullres.png'),
    });
  });
});
