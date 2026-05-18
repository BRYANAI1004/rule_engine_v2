# ruleengine_v2 — MCG source ingestion

This repository implements an **MCG (Medical Coverage Guidelines) source ingestion pipeline**: turning authorized guideline HTML snapshots into structured source trees, validating them, staging data in Supabase, and supporting downstream rule authoring workflows.

## Project layout

- `frontend/` — Vite + React placeholder UI (no real product UI yet)
- `backend/` — APIs and future source-ingestion services (minimal Express + existing TypeScript helpers)
- `tools/capture/` — Python Playwright capture scripts
- `tools/parse/` — Python HTML → `mcg_source_tree.v1` (Step 2A)
- `rules/mcg/` — local artifact directories (`raw-html`, `source-trees`, `domain-trees`, `audits`)
- `supabase/` — database migrations
- `docs/` — architecture and notes

The **backend** hosts HTTP APIs and future ingestion services. The **frontend** is currently a **placeholder** only.

**Supabase is not seeded** by this repo; migrations live under `supabase/migrations/`.

Python tooling under `tools/parse/` implements **Step 2A**: deterministic expanded-HTML → `mcg_source_tree.v1` JSON (no rule engine, no LLM).

## Step 0

Initializes the **TypeScript project skeleton**: tooling, env/path helpers, placeholder ingestion types, local folder layout for artifacts, and documentation of the planned pipeline.

## Step 1A (capture only)

**Python Playwright** scripts save authorized raw and expanded HTML under `rules/mcg/raw-html/` (gitignored) using a persistent local browser profile. This step does **not** parse HTML into JSON, build rules, or touch Supabase.

## Step 2A — source tree JSON (parse only)

After capture, convert expanded HTML into deterministic source-tree artifacts (no condition keys, no atomic/composite rules, no Supabase writes):

```bash
source .venv/bin/activate
pip install -r requirements.txt
python3 -m py_compile tools/parse/*.py
python tools/parse/build_mcg_source_tree.py \
  --mcg-code M083 \
  --title "Stroke: Ischemic" \
  --expanded-html rules/mcg/raw-html/M083.full.expanded.html \
  --out-dir rules/mcg/source-trees
python tools/parse/validate_mcg_source_tree.py \
  --input rules/mcg/source-trees/M083.source-tree.v1.json
```

Outputs land in `rules/mcg/source-trees/` (`*.v1.json`, JSONL shards, `*.audit.json`, roundtrip markdown).

## Step 2B — domain rule tree (deterministic, from validated source tree)

After **Step 2A** validation, convert the source tree into a **Level 0–3 domain tree** plus **Level 4 logic nodes** (no HTML re-parse, no LLM, no Supabase, no rule engine). This step is implemented for **M083** only.

```bash
source .venv/bin/activate
pip install -r requirements.txt
python3 -m py_compile tools/parse/*.py
python tools/parse/build_mcg_domain_rule_tree.py \
  --input rules/mcg/source-trees/M083.source-tree.v1.json \
  --out-dir rules/mcg/domain-trees
python tools/parse/validate_domain_rule_tree.py \
  --input rules/mcg/domain-trees/M083.domain-rule-tree.v1.json
```

Artifacts under `rules/mcg/domain-trees/` mirror Step 2A layout: main `*.v1.json`, JSONL shards, `*.audit.json`, and `*.roundtrip.md`.

## Licensed raw HTML

Raw MCG HTML may contain **licensed text**. Do **not** commit HTML snapshots or other proprietary source files to git. The `rules/mcg/raw-html/` tree is **gitignored** because it may contain licensed MCG content. Use the local directories under `rules/mcg/` only on authorized machines with appropriate license coverage.

## Setup

### Node / TypeScript (backend + frontend)

From the repository root:

```bash
npm install
```

**Backend** (health API + future services):

```bash
cd backend
npm install
npm restart
```

**Frontend** (placeholder):

```bash
cd frontend
npm install
npm restart
```

### Python (Step 1A capture + Step 2A parse)

Use a dedicated venv and install Playwright’s Chromium browser once:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Run capture (opens a **visible** browser with a persistent profile under `.local/playwright/mcg-careweb`; log in manually if prompted):

```bash
source .venv/bin/activate
python tools/capture/capture_mcg.py \
  --url "https://careweb.careguidelines.com/ed30/isc/0224500b.htm" \
  --mcg-code M083 \
  --title "Stroke: Ischemic" \
  --out-prefix M083
```

From the repo root you can also run `npm run capture:mcg --` with the same flags after `--`.

Do **not** commit captured HTML, screenshots, or audits; they are gitignored under `rules/mcg/raw-html/` and `rules/mcg/audits/`.

## Validation (from repo root)

```bash
npm run typecheck
npm run smoke
```

## Next step

Downstream steps will link Level 3 paths / Level 4 atomic–composite rules back to this source tree.

Pipeline overview: [docs/architecture/mcg-source-ingestion-v0.md](docs/architecture/mcg-source-ingestion-v0.md).
