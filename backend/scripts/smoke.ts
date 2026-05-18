import fs from 'node:fs';

import { env } from '../src/lib/env.js';
import { auditDir, projectRoot, rawHtmlDir, sourceTreeDir } from '../src/lib/paths.js';

function ensureDir(dir: string): void {
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
}

const supabaseConfigured =
  Boolean(env.SUPABASE_URL?.trim()) && Boolean(env.SUPABASE_SERVICE_ROLE_KEY?.trim());

console.log('Project root:', projectRoot);
console.log('Resolved raw-html dir:', rawHtmlDir);
console.log('Resolved source-trees dir:', sourceTreeDir);
console.log('Resolved audits dir:', auditDir);
console.log('Supabase env configured:', supabaseConfigured);

ensureDir(rawHtmlDir);
ensureDir(sourceTreeDir);
ensureDir(auditDir);

console.log('Smoke OK (directories ensured; Supabase not contacted).');
