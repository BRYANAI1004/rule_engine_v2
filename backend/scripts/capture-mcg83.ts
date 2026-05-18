import path from 'node:path';

import { captureMcgHtml } from '../src/mcg-ingestion/captureMcgHtml.js';
import { projectRoot } from '../src/lib/paths.js';

const defaultProfile = path.join(projectRoot, '.mcg-playwright-profile');
const userDataDir = process.env.MCG_BROWSER_USER_DATA_DIR ?? defaultProfile;

const result = await captureMcgHtml({
  scope: 'capture:M083',
  mcgId: 'M083',
  displayName: 'Stroke: Ischemic',
  url: 'https://careweb.careguidelines.com/ed30/isc/0224500b.htm',
  userDataDir,
  headless: process.env.MCG_HEADLESS === '1',
});

if (!result.ok) {
  process.exitCode = 1;
}
