import path from 'node:path';

import { type Page, chromium } from 'playwright';

import type { ProgressLogger } from '../lib/progressLogger.js';
import type { CaptureProgressState, McgCaptureConfig } from './mcgCaptureTypes.js';

const TEXT_TRUNCATE = 120;
const MAX_REMAINING_PREVIEW = 20;
const MAX_EXPAND_PASSES = 200;

export function truncateForLog(text: string, max = TEXT_TRUNCATE): string {
  const t = text.replace(/\s+/g, ' ').trim();
  if (t.length <= max) return t;
  return `${t.slice(0, max - 1)}…`;
}

function refreshElapsed(p: CaptureProgressState): void {
  p.elapsedSeconds = Math.floor((Date.now() - p.startedAt) / 1000);
}

type ExpandScan = {
  count: number;
  previews: string[];
};

async function scanExpandControls(page: Page): Promise<ExpandScan> {
  return page.evaluate((maxPreview) => {
    const texts: string[] = [];
    const els = Array.from(document.querySelectorAll('a, button, [role="button"], [role="link"]'));
    for (const el of els) {
      const raw = (el.textContent || '').replace(/\s+/g, ' ').trim();
      if (!/^Expand\b/i.test(raw)) continue;
      const style = window.getComputedStyle(el as HTMLElement);
      if (style.visibility === 'hidden' || style.display === 'none') continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      texts.push(raw);
    }
    return { count: texts.length, previews: texts.slice(0, maxPreview) };
  }, MAX_REMAINING_PREVIEW);
}

async function probeLoginNeeded(page: Page): Promise<{ loginLikely: boolean; detail: string }> {
  const url = page.url();
  if (/login|sign[-_]?(in|on)|auth|sso/i.test(url)) {
    return { loginLikely: true, detail: 'URL suggests auth flow' };
  }
  const title = await page.title();
  if (/sign\s*in|log\s*in/i.test(title)) {
    return { loginLikely: true, detail: 'Page title suggests login' };
  }
  const pwdCount = await page.locator('input[type="password"]:visible').count().catch(() => 0);
  if (pwdCount > 0) {
    return { loginLikely: true, detail: 'Visible password field' };
  }
  return { loginLikely: false, detail: 'No strong login heuristics matched' };
}

async function probePageContent(page: Page): Promise<Record<string, unknown>> {
  return page.evaluate(() => ({
    bodyTextLength: document.body?.innerText?.length ?? 0,
    linkCount: document.links.length,
    headingCount: document.querySelectorAll('h1,h2,h3').length,
    expandPatternMatches: Array.from(
      document.querySelectorAll('a,button,[role="button"],[role="link"]'),
    ).filter((el) => /^Expand\b/i.test((el.textContent || '').trim())).length,
  }));
}

async function expandAllSections(
  log: ProgressLogger,
  progress: CaptureProgressState,
  page: Page,
  recordWarn: (s: string) => void,
): Promise<{ expandPasses: number; expandClicks: number; remainingExpandControls: number }> {
  let pass = 0;
  let totalClicked = 0;

  while (pass < MAX_EXPAND_PASSES) {
    pass += 1;
    progress.expandPass = pass;
    progress.stage = 'expand';
    progress.lastAction = `expand_pass_${pass}_started`;
    refreshElapsed(progress);
    log.step(`Expand pass ${pass} started`);

    const scan = await scanExpandControls(page);
    log.info('Expand controls found', { candidateCount: scan.count });

    if (scan.count === 0) {
      log.success(`Expand pass ${pass} completed`, {
        clickedThisPass: 0,
        skippedThisPass: 0,
        warningThisPass: 0,
        totalClicked,
        remainingExpandControlCount: 0,
      });
      return { expandPasses: pass, expandClicks: totalClicked, remainingExpandControls: 0 };
    }

    let clickedThisPass = 0;
    let skippedThisPass = 0;
    let warningThisPass = 0;

    const loc = page.locator('a, button, [role="button"], [role="link"]').filter({ hasText: /^Expand\b/i });
    const initialCount = await loc.count();

    for (let i = 0; i < initialCount; i++) {
      const item = loc.nth(i);
      let label = '';
      try {
        const vis = await item.isVisible().catch(() => false);
        if (!vis) {
          skippedThisPass += 1;
          continue;
        }
        label = truncateForLog((await item.innerText()).replace(/\s+/g, ' ').trim());
        await item.scrollIntoViewIfNeeded().catch(() => undefined);
        await item.click({ timeout: 8000 });
        clickedThisPass += 1;
        totalClicked += 1;
        progress.expandClickCount = totalClicked;
        progress.lastAction = `clicked_expand_${totalClicked}`;
        refreshElapsed(progress);
        log.info('Clicked expand control', { index: clickedThisPass, text: label });
        await page.waitForTimeout(120);
      } catch (e) {
        const reason = e instanceof Error ? e.message : String(e);
        warningThisPass += 1;
        recordWarn(`Expand click failed — text: ${label || '(unknown)'} — ${reason}`);
        log.warn('Expand click failed', { text: label || '(unknown)', reason });
      }
    }

    const after = await scanExpandControls(page);
    log.success(`Expand pass ${pass} completed`, {
      clickedThisPass,
      skippedThisPass,
      warningThisPass,
      totalClicked,
      remainingExpandControlCount: after.count,
    });

    if (after.previews.length > 0) {
      log.info('Remaining expand controls (preview, first 20)', {
        remainingExpandControlCount: after.count,
        texts: after.previews.map((t) => truncateForLog(t)),
      });
    }

    progress.currentUrl = page.url();
    log.info('Current URL after expand pass', { url: page.url() });

    if (clickedThisPass === 0 && scan.count > 0) {
      log.warn('No expand controls clicked this pass; stopping to avoid infinite loop', {
        remainingExpandControlCount: after.count,
      });
      return { expandPasses: pass, expandClicks: totalClicked, remainingExpandControls: after.count };
    }

    if (after.count === 0) {
      return { expandPasses: pass, expandClicks: totalClicked, remainingExpandControls: 0 };
    }
  }

  log.warn('Expand pass limit reached', { maxPasses: MAX_EXPAND_PASSES });
  const finalScan = await scanExpandControls(page);
  return {
    expandPasses: MAX_EXPAND_PASSES,
    expandClicks: totalClicked,
    remainingExpandControls: finalScan.count,
  };
}

async function scrollFullPage(log: ProgressLogger, progress: CaptureProgressState, page: Page): Promise<void> {
  progress.stage = 'scroll';
  progress.lastAction = 'scroll_started';
  log.step('Full-page scroll started');
  await page.evaluate(() => window.scrollTo(0, 0));
  let scrollY = 0;
  let scrollHeight = await page.evaluate(() =>
    Math.max(document.body?.scrollHeight ?? 0, document.documentElement.scrollHeight),
  );
  const stepPx = 800;
  let stepIndex = 0;

  while (scrollY + 1 < scrollHeight) {
    scrollY = Math.min(scrollY + stepPx, scrollHeight);
    await page.evaluate((y) => window.scrollTo(0, y), scrollY);
    await page.waitForTimeout(80);
    stepIndex += 1;
    scrollHeight = await page.evaluate(() =>
      Math.max(document.body?.scrollHeight ?? 0, document.documentElement.scrollHeight),
    );
    if (stepIndex % 3 === 0 || scrollY >= scrollHeight - 1) {
      refreshElapsed(progress);
      log.info('Scrolling page', { scrollY, scrollHeight });
      progress.lastAction = `scroll_y_${scrollY}`;
    }
  }

  log.success('Full-page scroll completed', { finalScrollHeight: scrollHeight, finalScrollY: scrollY });
  progress.lastAction = 'scroll_completed';
}

export type RunMcgPageCaptureArgs = {
  config: McgCaptureConfig;
  log: ProgressLogger;
  progress: CaptureProgressState;
  recordWarn: (s: string) => void;
  outDir: string;
  waitForUser: (loginProbe: { loginLikely: boolean; detail: string }) => Promise<void>;
};

export async function runMcgPageCapture(args: RunMcgPageCaptureArgs): Promise<{
  pageTitle: string;
  finalUrl: string;
  rawHtml: string;
  expandedHtml: string;
  expandedText: string;
  expandPasses: number;
  expandClicks: number;
  remainingExpandControls: number;
  screenshotPath: string | null;
  loginProbe: { loginLikely: boolean; detail: string };
  probeBeforeWait: Record<string, unknown>;
  probeAfterExpand: Record<string, unknown>;
}> {
  const { config, log, progress, recordWarn, outDir, waitForUser } = args;

  log.step('Browser launch started', { headless: config.headless ?? false });
  progress.stage = 'browser_launch';
  progress.lastAction = 'browser_launch_started';

  const context = await chromium.launchPersistentContext(config.userDataDir, {
    headless: config.headless ?? false,
    viewport: { width: 1400, height: 900 },
  });

  log.info('Browser context userDataDir', { userDataDir: config.userDataDir });

  const page = await context.newPage();
  log.step('Page open started', { url: config.url });
  progress.stage = 'navigate';
  progress.lastAction = 'goto_started';

  await page.goto(config.url, { waitUntil: 'load', timeout: 120_000 });
  progress.currentUrl = page.url();
  refreshElapsed(progress);
  log.info('URL loaded', { url: page.url() });
  log.info('Current page URL', { currentUrl: page.url() });

  let pageTitle = await page.title();
  log.info('Page title (initial)', { title: pageTitle });

  const loginProbe = await probeLoginNeeded(page);
  log.info('Login / manual intervention probe', {
    loginOrManualLikely: loginProbe.loginLikely,
    detail: loginProbe.detail,
  });

  const probeBeforeWait = await probePageContent(page);
  log.info('Page content probe (before user continue)', probeBeforeWait);

  await waitForUser(loginProbe);

  progress.stage = 'post_wait_probe';
  progress.currentUrl = page.url();
  pageTitle = await page.title();
  log.info('Page title (after user continue)', { title: pageTitle });

  const probeAfterWait = await probePageContent(page);
  log.info('Page content probe (after user continue)', probeAfterWait);

  const rawHtml = await page.content();
  log.info('Raw HTML snapshot captured (pre-expand)', { byteLength: rawHtml.length });
  progress.lastAction = 'raw_html_captured_in_memory';

  const expandResult = await expandAllSections(log, progress, page, recordWarn);
  await scrollFullPage(log, progress, page);

  const expandedHtml = await page.content();
  const expandedText =
    (await page.evaluate(() => document.body?.innerText ?? '')) || '';

  progress.stage = 'screenshot';
  progress.lastAction = 'screenshot_started';
  const screenshotPath = path.join(outDir, 'full-page.png');
  log.step('Screenshot capture started', { path: screenshotPath });
  let savedShot: string | null = null;
  try {
    await page.screenshot({ path: screenshotPath, fullPage: true, timeout: 120_000 });
    savedShot = screenshotPath;
    log.success('Screenshot saved', { path: screenshotPath });
    progress.lastAction = 'screenshot_saved';
  } catch (e) {
    const reason = e instanceof Error ? e.message : String(e);
    recordWarn(`Screenshot failed — ${reason}`);
    log.warn('Screenshot failed', { path: screenshotPath, reason });
    progress.lastAction = 'screenshot_failed';
  }

  const probeAfterExpand = await probePageContent(page);
  log.info('Page content probe (after expand + scroll)', probeAfterExpand);

  const finalUrl = page.url();
  progress.currentUrl = finalUrl;
  await context.close();

  return {
    pageTitle,
    finalUrl,
    rawHtml,
    expandedHtml,
    expandedText,
    expandPasses: expandResult.expandPasses,
    expandClicks: expandResult.expandClicks,
    remainingExpandControls: expandResult.remainingExpandControls,
    screenshotPath: savedShot,
    loginProbe,
    probeBeforeWait,
    probeAfterExpand,
  };
}
