import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import dotenv from 'dotenv';
import { z } from 'zod';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const backendRootForEnv = path.resolve(__dirname, '../..');
const repositoryRootForEnv = path.resolve(backendRootForEnv, '..');

function loadDotenvFiles(): void {
  const repoEnv = path.join(repositoryRootForEnv, '.env');
  const backendEnv = path.join(backendRootForEnv, '.env');

  if (fs.existsSync(repoEnv)) {
    dotenv.config({ path: repoEnv });
  }
  // Local backend overrides repo defaults when present
  if (fs.existsSync(backendEnv)) {
    dotenv.config({ path: backendEnv, override: true });
  }
}

loadDotenvFiles();

const envSchema = z.object({
  SUPABASE_URL: z.string().optional(),
  SUPABASE_SERVICE_ROLE_KEY: z.string().optional(),
  MCG_RAW_HTML_DIR: z.string().default('rules/mcg/raw-html'),
  MCG_SOURCE_TREE_DIR: z.string().default('rules/mcg/source-trees'),
  MCG_AUDIT_DIR: z.string().default('rules/mcg/audits'),
});

export type Env = z.infer<typeof envSchema>;

export const env: Env = envSchema.parse({
  SUPABASE_URL: emptyToUndefined(process.env.SUPABASE_URL),
  SUPABASE_SERVICE_ROLE_KEY: emptyToUndefined(process.env.SUPABASE_SERVICE_ROLE_KEY),
  MCG_RAW_HTML_DIR: emptyToUndefined(process.env.MCG_RAW_HTML_DIR),
  MCG_SOURCE_TREE_DIR: emptyToUndefined(process.env.MCG_SOURCE_TREE_DIR),
  MCG_AUDIT_DIR: emptyToUndefined(process.env.MCG_AUDIT_DIR),
});

function emptyToUndefined(value: string | undefined): string | undefined {
  if (value === undefined || value.trim() === '') {
    return undefined;
  }
  return value;
}
