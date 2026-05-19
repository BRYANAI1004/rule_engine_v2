# ruleengine_v2 — MCG source ingestion

This repository implements an **MCG (Medical Coverage Guidelines) source ingestion pipeline**: turning authorized guideline HTML snapshots into structured source trees, validating them, staging data in Supabase, and supporting downstream rule authoring workflows.

## Project layout

- `frontend/` — Vite + React placeholder UI (no real product UI yet)
- `backend/` — APIs and future source-ingestion services (minimal Express + existing TypeScript helpers)
- `tools/capture/` — Python Playwright capture scripts
- `tools/parse/` — Python HTML → `mcg_source_tree.v1` (Step 2A)
- `rules/mcg/` — local artifact directories (`raw-html`, `source-trees`, `domain-trees`, `shared-condition-definitions`, `linked-rule-trees`, `audits`)
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

## Step 2D — shared condition definitions (from definition popup captures)

After **Step 1A/1B definition capture**, convert selected `mcg_popup_capture.v2` definition popups into `mcg_shared_condition_definitions.v1` JSON (deterministic, targeted parser; no LLM; no Supabase; no rule-engine evaluation).

```bash
source .venv/bin/activate

python tools/parse/build_mcg_definition_rule_tree.py \
  --mcg-code M083 \
  --title "Stroke: Ischemic" \
  --definitions-json rules/mcg/raw-html/M083.definitions.raw.json \
  --domain-rule-tree rules/mcg/domain-trees/M083.domain-rule-tree.v1.json \
  --out-dir rules/mcg/shared-condition-definitions

python tools/parse/validate_mcg_definition_rule_tree.py \
  --input rules/mcg/shared-condition-definitions/M083.shared-condition-definitions.v1.json
```

Artifacts under `rules/mcg/shared-condition-definitions/` include the main JSON, JSONL shards, `*.audit.json`, and `*.roundtrip.md`.

DuckDB examples:

```bash
duckdb -c "
select condition_key, display_name, root_composite_id
from read_ndjson_auto('rules/mcg/shared-condition-definitions/M083.shared-condition-definitions.conditions.jsonl');
"

duckdb -c "
select id, condition_key, measurement, operator, value, unit, original_text
from read_ndjson_auto('rules/mcg/shared-condition-definitions/M083.shared-condition-definitions.atomic-rules.jsonl')
where condition_key in (
  'systolic_blood_pressure_mmhg','shock_index','mean_arterial_pressure_mmhg',
  'lactate_mmol_l','arterial_or_venous_ph'
);
"
```

## Step 2E — linked rule tree (domain logic ↔ shared definitions)

Project the **domain rule tree** onto **shared condition definitions**: each Level 4 logic node keeps its original fields plus `definition_link_status` (`linked_shared_definition`, `unlinked`, or `not_applicable`). This is a read-only linkage artifact (it does not replace the domain tree).

```bash
source .venv/bin/activate
PYTHONPYCACHEPREFIX="$(pwd)/.pyc_tmp" python3 -m py_compile tools/parse/*.py
rm -rf .pyc_tmp

python tools/parse/build_mcg_linked_rule_tree.py \
  --mcg-code M083 \
  --domain-rule-tree rules/mcg/domain-trees/M083.domain-rule-tree.v1.json \
  --shared-definitions rules/mcg/shared-condition-definitions/M083.shared-condition-definitions.v1.json \
  --out-dir rules/mcg/linked-rule-trees

python tools/parse/validate_mcg_linked_rule_tree.py \
  --input rules/mcg/linked-rule-trees/M083.linked-rule-tree.v1.json \
  --shared-definitions rules/mcg/shared-condition-definitions/M083.shared-condition-definitions.v1.json
```

Artifacts under `rules/mcg/linked-rule-trees/` include `*.v1.json`, `*.linked-logic-nodes.jsonl`, `*.linked-condition-refs.jsonl`, `*.audit.json`, and `*.roundtrip.md`.

DuckDB examples:

```bash
duckdb -c "
select
  condition_key,
  definition_link_status,
  root_composite_id,
  domain_original_text
from read_ndjson_auto('rules/mcg/linked-rule-trees/M083.linked-condition-refs.jsonl')
where condition_key = 'hemodynamic_instability_condition_present';
"
```

### Export integrated admission hierarchy (PDF)

From a domain rule tree, shared definitions, and linked condition refs, emit the same styled HTML/PDF used for admission path review (`tools/export/export_mcg_integrated_admission_pdf.py` wraps Playwright/Chromium):

```bash
python tools/export/export_mcg_integrated_admission_pdf.py \
  --mcg-code M083 \
  --mcg-title "Stroke: Ischemic" \
  --domain-rule-tree rules/mcg/domain-trees/M083.domain-rule-tree.v1.json \
  --shared-definitions rules/mcg/shared-condition-definitions/M083.shared-condition-definitions.v1.json \
  --linked-condition-refs rules/mcg/linked-rule-trees/M083.linked-condition-refs.jsonl \
  --output rules/mcg/previews/M083.integrated-rule-hierarchy.pdf
```

The legacy shim `tools/export/export_m083_integrated_admission_pdf.py` keeps the prior CLI (`--domain-rule-tree`, `--shared-definitions`, `--linked-condition-refs`, `--output`, optional `--title`) and forwards to the generic exporter with M083 code/title.

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
