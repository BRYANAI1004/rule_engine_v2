export type ProgressLogData = Record<string, unknown>;

export type ProgressLogger = {
  readonly scope: string;
  info(message: string, data?: ProgressLogData): void;
  step(message: string, data?: ProgressLogData): void;
  success(message: string, data?: ProgressLogData): void;
  warn(message: string, data?: ProgressLogData): void;
  error(message: string, data?: ProgressLogData): void;
};

type LogLevel = 'INFO' | 'STEP' | 'SUCCESS' | 'WARN' | 'ERROR';

function formatLine(scope: string, level: LogLevel, message: string, data?: ProgressLogData): string {
  const ts = new Date().toISOString();
  const tail = data !== undefined ? ` ${JSON.stringify(data)}` : '';
  return `[${ts}] [${scope}] [${level}] ${message}${tail}`;
}

/** Terminal structured logger: timestamp, scope, level, message, optional JSON payload. */
export function createProgressLogger(scope: string): ProgressLogger {
  const write = (level: LogLevel, message: string, data?: ProgressLogData): void => {
    console.log(formatLine(scope, level, message, data));
  };

  return {
    scope,
    info(message, data) {
      write('INFO', message, data);
    },
    step(message, data) {
      write('STEP', message, data);
    },
    success(message, data) {
      write('SUCCESS', message, data);
    },
    warn(message, data) {
      write('WARN', message, data);
    },
    error(message, data) {
      write('ERROR', message, data);
    },
  };
}
