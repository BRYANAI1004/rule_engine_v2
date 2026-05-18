import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { env } from './env.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** Backend package root (`backend/` in the repository). */
export const backendRoot = path.resolve(__dirname, '../..');

/**
 * Repository (monorepo) root — parent of `backend/`.
 * Artifact paths (`rules/mcg/...`) resolve from here (e.g. `../rules` when cwd is `backend/`).
 */
export const repositoryRoot = path.resolve(backendRoot, '..');

/** @deprecated Prefer `repositoryRoot`. */
export const projectRoot = repositoryRoot;

export const rawHtmlDir = path.resolve(repositoryRoot, env.MCG_RAW_HTML_DIR);
export const sourceTreeDir = path.resolve(repositoryRoot, env.MCG_SOURCE_TREE_DIR);
export const auditDir = path.resolve(repositoryRoot, env.MCG_AUDIT_DIR);
