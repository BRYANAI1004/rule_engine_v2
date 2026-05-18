import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';

import { createProgressLogger } from '../lib/progressLogger.js';
import { rawHtmlDir } from '../lib/paths.js';
import type { CaptureProgressState, McgCaptureConfig } from './mcgCaptureTypes.js';
import { runMcgPageCapture } from './mcgPageCapture.js';

export type { CaptureProgressState, McgCaptureConfig } from './mcgCaptureTypes.js';

export type McgCaptureFullConfig = McgCaptureConfig & {
  /** Directory for this guideline's artifacts (default: rawHtmlDir / mcgId) */
  outputDir?: string;
};

export type McgCaptureArtifacts = {
  rawHtmlPath: string;
  expandedHtmlPath: string;
  expandedTextPath: string;
  manifestPath: string;
  summaryPath: string;
  screenshotPath: string | null;
};

export type McgCaptureResult = {
  ok: true;
  pageTitle: string;
  finalUrl: string;
  expandPasses: number;
  expandClicks: number;
  remainingExpandControls: number;
  elapsedSeconds: number;
  warnings: string[];
  probe: Record<string, unknown>;
  artifacts: McgCaptureArtifacts;
};

export type McgCaptureFailure = {
  ok: false;
  error: unknown;
  partial: Partial<McgCaptureArtifacts>;
  warnings: string[];
};

function createInitialProgress(currentUrl: string): CaptureProgressState {
  return {
    stage: 'init',
    startedAt: Date.now(),
    elapsedSeconds: 0,
    currentUrl,
    expandPass: 0,
    expandClickCount: 0,
    warningCount: 0,
    lastAction: 'initialized',
  };
}

function refreshElapsed(p: CaptureProgressState): void {
  p.elapsedSeconds = Math.floor((Date.now() - p.startedAt) / 1000);
}

function startHeartbeat(
  log: ReturnType<typeof createProgressLogger>,
  getProgress: () => CaptureProgressState,
  intervalMs: number,
): () => void {
  const id = setInterval(() => {
    const p = getProgress();
    refreshElapsed(p);
    log.info('Still running', {
      stage: p.stage,
      elapsedSeconds: p.elapsedSeconds,
      expandPass: p.expandPass,
      totalClicked: p.expandClickCount,
    });
  }, intervalMs);
  return () => clearInterval(id);
}

function waitForEnter(log: ReturnType<typeof createProgressLogger>, progress: CaptureProgressState): Promise<void> {
  if (!process.stdin.isTTY) {
    log.warn('stdin is not a TTY; continuing without waiting for Enter', {});
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    progress.stage = 'waiting_for_enter';
    progress.lastAction = 'waiting_for_user_enter';
    log.step('Waiting for Enter in terminal to continue capture');
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    rl.question('Press Enter after login / page is ready…\n', () => {
      rl.close();
      log.info('User continued after Enter', {});
      progress.lastAction = 'user_continued_after_enter';
      resolve();
    });
  });
}

function boxLine(s: string, width: number): string {
  const inner = ` ${s} `;
  if (inner.length >= width - 2) {
    return `=${s}=`;
  }
  const pad = width - inner.length - 2;
  const left = Math.floor(pad / 2);
  const right = pad - left;
  return `=${'='.repeat(left)}${inner}${'='.repeat(right)}=`;
}

function printCompleteSummary(args: {
  mcgId: string;
  displayName: string;
  finalUrl: string;
  elapsedSeconds: number;
  expandPasses: number;
  expandClicks: number;
  remainingExpandControls: number;
  artifacts: McgCaptureArtifacts;
  warnings: string[];
}): void {
  const w = 52;
  const lines = [
    boxLine('MCG CAPTURE COMPLETE', w),
    `${args.mcgId} ${args.displayName}`,
    `URL: ${args.finalUrl}`,
    `Elapsed: ${args.elapsedSeconds} seconds`,
    `Expand passes: ${args.expandPasses}`,
    `Expand clicks: ${args.expandClicks}`,
    `Remaining expand controls: ${args.remainingExpandControls}`,
    `Raw HTML: ${args.artifacts.rawHtmlPath}`,
    `Expanded HTML: ${args.artifacts.expandedHtmlPath}`,
    `Expanded text: ${args.artifacts.expandedTextPath}`,
    `Manifest: ${args.artifacts.manifestPath}`,
    `Summary: ${args.artifacts.summaryPath}`,
    `Screenshot: ${args.artifacts.screenshotPath ?? '(none)'}`,
    `Warnings: ${args.warnings.length}`,
    '='.repeat(w),
  ];
  console.log(`\n${lines.join('\n')}\n`);
}

function printFailureSummary(args: {
  stage: string;
  elapsedSeconds: number;
  lastAction: string;
  currentUrl: string;
  error: unknown;
  warnings: string[];
  partialPaths: string[];
}): void {
  const w = 52;
  const errMsg = args.error instanceof Error ? args.error.message : String(args.error);
  const lines = [
    boxLine('MCG CAPTURE FAILED', w),
    `Stage: ${args.stage}`,
    `Elapsed: ${args.elapsedSeconds} seconds`,
    `Last action: ${args.lastAction}`,
    `Current URL: ${args.currentUrl}`,
    `Error: ${errMsg}`,
    `Warnings: ${args.warnings.length}`,
    `Partial outputs saved: ${args.partialPaths.length ? args.partialPaths.join('; ') : '(none)'}`,
    '='.repeat(w),
  ];
  console.log(`\n${lines.join('\n')}\n`);
}

function assessManualIntervention(
  log: ReturnType<typeof createProgressLogger>,
  progress: CaptureProgressState,
  loginProbe: { loginLikely: boolean; detail: string },
): void {
  progress.stage = 'login_assessment';
  progress.lastAction = 'assessed_login_probe';
  log.info('Login / manual intervention assessment', {
    loginOrManualLikely: loginProbe.loginLikely,
    detail: loginProbe.detail,
  });
  if (loginProbe.loginLikely) {
    log.step('Manual login or page setup may be required', { loginOrManualLikely: true });
  }
}

/**
 * Step 1A: capture expanded MCG HTML into `rules/mcg/raw-html/<mcgId>/` (by default).
 */
export async function captureMcgHtml(config: McgCaptureFullConfig): Promise<McgCaptureResult | McgCaptureFailure> {
  const log = createProgressLogger(config.scope);
  const outDir = config.outputDir ?? path.join(rawHtmlDir, config.mcgId);
  const progress = createInitialProgress(config.url);
  const warnings: string[] = [];
  const partial: Partial<McgCaptureArtifacts> = {};

  const stopHeartbeat = startHeartbeat(log, () => progress, 10_000);

  const recordWarn = (msg: string): void => {
    warnings.push(msg);
    progress.warningCount = warnings.length;
  };

  try {
    if (!fs.existsSync(outDir)) {
      fs.mkdirSync(outDir, { recursive: true });
    }

    log.info('Output directory', { outputDir: outDir });

    const {
      pageTitle,
      finalUrl,
      rawHtml,
      expandedHtml,
      expandedText,
      expandPasses,
      expandClicks,
      remainingExpandControls,
      screenshotPath,
      loginProbe,
      probeBeforeWait,
      probeAfterExpand,
    } = await runMcgPageCapture({
      config,
      log,
      progress,
      recordWarn,
      outDir,
      waitForUser: async (probe) => {
        assessManualIntervention(log, progress, probe);
        await waitForEnter(log, progress);
      },
    });

    partial.rawHtmlPath = path.join(outDir, 'raw.html');
    partial.expandedHtmlPath = path.join(outDir, 'expanded.html');
    partial.expandedTextPath = path.join(outDir, 'expanded.txt');
    partial.screenshotPath = screenshotPath;
    partial.manifestPath = path.join(outDir, 'capture-manifest.json');
    partial.summaryPath = path.join(outDir, 'capture-summary.md');

    fs.writeFileSync(partial.rawHtmlPath, rawHtml, 'utf8');
    log.success('Raw HTML saved', { path: partial.rawHtmlPath });

    fs.writeFileSync(partial.expandedHtmlPath, expandedHtml, 'utf8');
    log.success('Expanded HTML saved', { path: partial.expandedHtmlPath });

    fs.writeFileSync(partial.expandedTextPath, expandedText, 'utf8');
    log.success('Expanded text saved', { path: partial.expandedTextPath });

    refreshElapsed(progress);
    const elapsedSeconds = progress.elapsedSeconds;

    const artifacts: McgCaptureArtifacts = {
      rawHtmlPath: partial.rawHtmlPath,
      expandedHtmlPath: partial.expandedHtmlPath,
      expandedTextPath: partial.expandedTextPath,
      manifestPath: partial.manifestPath,
      summaryPath: partial.summaryPath,
      screenshotPath: partial.screenshotPath ?? null,
    };

    const manifest = {
      mcgId: config.mcgId,
      displayName: config.displayName,
      url: config.url,
      finalUrl,
      pageTitle,
      capturedAt: new Date().toISOString(),
      elapsedSeconds,
      expandPasses,
      expandClicks,
      remainingExpandControls,
      loginProbe,
      probeBeforeWait,
      probeAfterExpand,
      warnings,
      files: {
        rawHtml: path.relative(process.cwd(), artifacts.rawHtmlPath),
        expandedHtml: path.relative(process.cwd(), artifacts.expandedHtmlPath),
        expandedText: path.relative(process.cwd(), artifacts.expandedTextPath),
        manifest: path.relative(process.cwd(), artifacts.manifestPath),
        summary: path.relative(process.cwd(), artifacts.summaryPath),
        screenshot: artifacts.screenshotPath
          ? path.relative(process.cwd(), artifacts.screenshotPath)
          : null,
      },
    };

    fs.writeFileSync(artifacts.manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
    log.success('Manifest saved', { path: artifacts.manifestPath });

    const summaryMd = [
      `# MCG capture — ${config.mcgId} ${config.displayName}`,
      '',
      `- URL: ${config.url}`,
      `- Final URL: ${finalUrl}`,
      `- Page title: ${pageTitle}`,
      `- Captured at: ${manifest.capturedAt}`,
      `- Elapsed: ${elapsedSeconds}s`,
      `- Expand passes: ${expandPasses}`,
      `- Expand clicks: ${expandClicks}`,
      `- Remaining expand controls: ${remainingExpandControls}`,
      `- Warnings: ${warnings.length}`,
      '',
      '## Artifacts',
      '',
      ...Object.entries(manifest.files).map(([k, v]) => `- **${k}**: ${v}`),
      '',
    ].join('\n');

    fs.writeFileSync(artifacts.summaryPath, summaryMd, 'utf8');
    log.success('Summary markdown saved', { path: artifacts.summaryPath });

    progress.stage = 'complete';
    progress.lastAction = 'capture_complete';
    log.step('Capture complete', {
      expandPasses,
      expandClicks,
      remainingExpandControls,
      elapsedSeconds,
    });

    printCompleteSummary({
      mcgId: config.mcgId,
      displayName: config.displayName,
      finalUrl,
      elapsedSeconds,
      expandPasses,
      expandClicks,
      remainingExpandControls,
      artifacts,
      warnings,
    });

    return {
      ok: true,
      pageTitle,
      finalUrl,
      expandPasses,
      expandClicks,
      remainingExpandControls,
      elapsedSeconds,
      warnings,
      probe: probeAfterExpand,
      artifacts,
    };
  } catch (err) {
    refreshElapsed(progress);
    const partialPaths = Object.values(partial).filter((p): p is string => typeof p === 'string' && fs.existsSync(p));
    printFailureSummary({
      stage: progress.stage,
      elapsedSeconds: progress.elapsedSeconds,
      lastAction: progress.lastAction,
      currentUrl: progress.currentUrl,
      error: err,
      warnings,
      partialPaths,
    });
    log.error('Capture failed', {
      error: err instanceof Error ? err.message : String(err),
    });
    return { ok: false, error: err, partial, warnings };
  } finally {
    stopHeartbeat();
  }
}
