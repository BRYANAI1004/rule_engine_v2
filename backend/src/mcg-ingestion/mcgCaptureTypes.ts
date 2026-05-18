export type CaptureProgressState = {
  stage: string;
  startedAt: number;
  elapsedSeconds: number;
  currentUrl: string;
  expandPass: number;
  expandClickCount: number;
  warningCount: number;
  lastAction: string;
};

export type McgCaptureConfig = {
  /** Logger scope, e.g. capture:M083 */
  scope: string;
  mcgId: string;
  displayName: string;
  url: string;
  /** Playwright persistent profile directory */
  userDataDir: string;
  headless?: boolean;
};
