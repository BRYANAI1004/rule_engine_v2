# MCG source ingestion pipeline (v0)

High-level stages for turning guideline HTML into reviewable, publishable rule data.

## Intended pipeline

**Step 0 — Project init**  
TypeScript skeleton, env/path utilities, placeholder types, local artifact folders, docs.

**Step 1 — HTML snapshot**  
Store authorized MCG HTML snapshots locally (not committed). Treat content as licensed.

**Step 2 — HTML → source tree JSON**  
Parse HTML into a canonical **source tree** JSON representation suitable for validation and staging.

**Step 3 — Validate + round-trip audit**  
Schema validation, consistency checks, and audits that prove the tree round-trips safely against expectations.

**Step 4 — Supabase source staging**  
Load validated source packs into staging tables for downstream workflows.

**Step 5 — Bot condition proposal**  
Automated proposals for condition structures from staged source (human-in-the-loop later).

**Step 6 — Validator / reviewer**  
Human or tooling-assisted review of proposals vs source evidence.

**Step 7 — Publish rule database**  
Promote accepted artifacts into the production-oriented rule store.

This document describes intent only; Step 0 does not implement these stages beyond scaffolding.
