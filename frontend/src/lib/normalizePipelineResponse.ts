import type {
  McgPipelineApiEnvelope,
  PipelineCriterionRow,
  PipelineFactRecord,
  PipelineRouterCandidate,
  UiAdmission,
  UiCriterionRow,
  UiFactRow,
  UiPipelineViewModel,
  UiRouterRow,
} from './pipelineTypes';

function asRecord(x: unknown): Record<string, unknown> | null {
  return x !== null && typeof x === 'object' && !Array.isArray(x)
    ? (x as Record<string, unknown>)
    : null;
}

function safeStr(x: unknown): string | undefined {
  if (x === null || x === undefined) return undefined;
  if (typeof x === 'string') return x;
  if (typeof x === 'number' || typeof x === 'boolean') return String(x);
  return undefined;
}

let _id = 0;
function nextId(prefix: string): string {
  _id += 1;
  return `${prefix}-${_id}`;
}

function joinSignals(c: PipelineRouterCandidate): string {
  const parts: string[] = [];
  const push = (arr: unknown) => {
    if (!Array.isArray(arr)) return;
    for (const item of arr) {
      const s = safeStr(item);
      if (s) parts.push(s);
    }
  };
  push(c.matched_routing_signals);
  push(c.matched_signals);
  push(c.routing_signals);
  push(c.matched_terms);
  push(c.terms);
  const inline = parts.length ? parts.join(' · ') : '';
  return inline;
}

function pickRouterList(env: McgPipelineApiEnvelope): PipelineRouterCandidate[] {
  const a = env.router_candidates ?? env.router_candidate_strip ?? env.rankings;
  if (Array.isArray(a)) return a;
  return [];
}

function pickRankingsFull(env: McgPipelineApiEnvelope): PipelineRouterCandidate[] {
  const a =
    env.full_router_rankings ??
    env.router_rankings ??
    env.rankings ??
    env.router_candidates;
  if (Array.isArray(a)) return a;
  return [];
}

function mapRouterRow(c: PipelineRouterCandidate, i: number): UiRouterRow {
  const code = safeStr(c.mcg_code ?? c.code ?? c.module_code) ?? `row-${i}`;
  const title = safeStr(c.title ?? c.name);
  const score = typeof c.score === 'number' ? c.score : typeof c.routing_score === 'number' ? c.routing_score : undefined;
  const strength = safeStr(c.strength ?? c.signal_strength);
  return {
    id: nextId('rt'),
    mcgCode: code,
    title,
    score,
    strength,
    signalsInline: joinSignals(c),
  };
}

function extractFactsArray(env: McgPipelineApiEnvelope): PipelineFactRecord[] {
  const nm = env.normalized_for_matching;
  if (Array.isArray(nm)) return nm as PipelineFactRecord[];
  if (nm && typeof nm === 'object' && !Array.isArray(nm)) {
    const out: PipelineFactRecord[] = [];
    for (const [k, v] of Object.entries(nm)) {
      if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
        out.push({ ...(v as PipelineFactRecord), condition_key: (v as PipelineFactRecord).condition_key ?? k });
      } else {
        out.push({ condition_key: k, value: safeStr(v) });
      }
    }
    return out;
  }
  const lists = [env.normalized_facts, env.extracted_facts, env.facts, env.llm_facts];
  for (const lst of lists) {
    if (Array.isArray(lst)) return lst as PipelineFactRecord[];
  }
  return [];
}

function mapFactRow(r: PipelineFactRecord, _i: number): UiFactRow {
  const conditionKey =
    safeStr(r.condition_key ?? r.normalized_condition_key ?? r.key) ?? undefined;
  const factText =
    safeStr(r.fact ?? r.text ?? r.normalized_fact ?? r.normalized) ??
    (conditionKey ? safeStr(r.value) : undefined) ??
    (conditionKey ? conditionKey : '—');
  const mapped = Boolean(conditionKey) && r.mapped !== false;
  return {
    id: nextId('f'),
    factText,
    conditionKey,
    mapped,
    value: safeStr(r.value ?? r.measurement),
    evidence: safeStr(r.evidence ?? r.evidence_quote ?? r.quote ?? r.span),
    source: safeStr(r.source ?? r.source_field),
  };
}

function normalizeAdmission(raw: unknown, re: Record<string, unknown> | null): UiAdmission {
  const tryVal = (v: unknown): UiAdmission | null => {
    const s = safeStr(v)?.toUpperCase();
    if (!s) return null;
    if (s === 'YES' || s === 'Y' || s === 'TRUE' || s === 'MET' || s === 'ADMIT') return 'YES';
    if (s === 'NO' || s === 'N' || s === 'FALSE' || s === 'NOT_MET' || s === 'DENY') return 'NO';
    if (s === 'UNKNOWN' || s === 'UNCERTAIN' || s === 'INDETERMINATE') return 'UNKNOWN';
    return null;
  };

  const direct =
    tryVal(re?.admission) ??
    tryVal(re?.admission_recommendation) ??
    tryVal(re?.admission_decision) ??
    tryVal(re?.recommendation) ??
    tryVal(asRecord(raw)?.admission) ??
    tryVal(asRecord(raw)?.admission_decision);

  if (direct) return direct;

  const ar = asRecord(asRecord(raw)?.['admission_recommendation']);
  if (ar) {
    const inner = tryVal(ar['value'] ?? ar['status'] ?? ar['code'] ?? ar['result']);
    if (inner) return inner;
  }

  return 'UNKNOWN';
}

function rationaleFrom(env: McgPipelineApiEnvelope, re: Record<string, unknown> | null): string | undefined {
  const rer = asRecord(env.rule_engine_result);
  return (
    safeStr(re?.rationale) ??
    safeStr(re?.summary) ??
    safeStr(re?.admission_rationale) ??
    safeStr(rer?.['rationale']) ??
    safeStr((env.rule_engine as Record<string, unknown> | undefined)?.['rationale'])
  );
}

function criterionLabel(row: PipelineCriterionRow): string {
  return (
    safeStr(row.criterion) ??
    safeStr(row.condition) ??
    safeStr(row.label) ??
    safeStr(row.name) ??
    safeStr(row.description) ??
    'Criterion'
  );
}

function criterionResultToken(row: PipelineCriterionRow): string {
  const r =
    safeStr(row.result)?.toUpperCase() ??
    safeStr(row.trilean)?.toUpperCase() ??
    (typeof row.matched === 'boolean' ? (row.matched ? 'TRUE' : 'FALSE') : undefined);
  if (!r) return 'UNKNOWN';
  if (r === 'MATCHED' || r === 'MET') return 'TRUE';
  return r;
}

function isInternalNoise(row: PipelineCriterionRow): boolean {
  const st = safeStr(row.review_status ?? row.internal_status)?.toLowerCase();
  return st === 'needs_review' || st === 'unresolved' || st === 'internal';
}

function mapCriterionRow(row: PipelineCriterionRow): UiCriterionRow {
  const res = criterionResultToken(row);
  return {
    id: nextId('c'),
    label: criterionLabel(row),
    result: res,
    evidence: safeStr(row.evidence ?? row.evidence_quote),
    logicSummary: safeStr(row.rule_logic_summary ?? row.logic_summary ?? row.rationale),
    logicTree: row.logic_tree ?? row.human_logic,
    ruleJson: row.runtime_rule ?? row.rule,
    sourceCriteriaText: safeStr(row.source_criteria_text ?? row.criteria_source_text),
    isNoise: isInternalNoise(row),
  };
}

function gatherCriteria(env: McgPipelineApiEnvelope): PipelineCriterionRow[] {
  const lists = [env.evaluation_rows, env.criteria_evaluations, env.evaluations];
  for (const lst of lists) {
    if (Array.isArray(lst) && lst.length) return lst as PipelineCriterionRow[];
  }
  const re =
    (asRecord(env.rule_engine) as Record<string, unknown> | undefined) ??
    (asRecord(env.deterministic_rule_engine) as Record<string, unknown> | undefined) ??
    (asRecord(env.rule_engine_result) as Record<string, unknown> | undefined);
  if (re) {
    for (const k of ['criteria', 'top_criteria', 'matched_criteria', 'rows']) {
      const v = re[k];
      if (Array.isArray(v) && v.length) return v as PipelineCriterionRow[];
    }
  }
  return [];
}

const TRUEISH = new Set(['TRUE', 'YES', 'MET', 'MATCHED', 'Y']);
const UNKNOWNISH = new Set(['UNKNOWN', 'UNCERTAIN', 'INDETERMINATE']);

function partitionCriteria(rows: UiCriterionRow[]): {
  matched: UiCriterionRow[];
  unknowns: UiCriterionRow[];
  stats: { evaluated: number; matched: number; unknown: number };
} {
  const visible = rows.filter((r) => !r.isNoise);
  let matched = 0;
  let unknown = 0;
  for (const r of visible) {
    const u = r.result.toUpperCase();
    if (TRUEISH.has(u)) matched += 1;
    else if (UNKNOWNISH.has(u) || u === 'UNRESOLVED') unknown += 1;
  }
  const m = visible.filter((r) => TRUEISH.has(r.result.toUpperCase())).slice(0, 3);
  const u = visible
    .filter((r) => UNKNOWNISH.has(r.result.toUpperCase()) && !TRUEISH.has(r.result.toUpperCase()))
    .slice(0, 2);
  return {
    matched: m,
    unknowns: u,
    stats: { evaluated: visible.length, matched, unknown },
  };
}

function pickSelectedModule(
  env: McgPipelineApiEnvelope,
  routerTop: UiRouterRow[],
): { code: string; title?: string } | null {
  const fallbackRow = routerTop[0];
  const code =
    safeStr(env.selected_mcg_code) ??
    safeStr(env.selected_module?.code) ??
    safeStr(env.module?.code) ??
    safeStr(fallbackRow?.mcgCode);
  if (!code) return null;
  const title =
    safeStr(env.selected_module?.title ?? env.selected_module?.name) ??
    safeStr(env.module?.title) ??
    routerTop.find((r) => r.mcgCode === code)?.title ??
    fallbackRow?.title;
  return { code, title };
}

function statsFrom(env: McgPipelineApiEnvelope, factsLen: number, part: { evaluated: number; matched: number; unknown: number }): UiPipelineViewModel['stats'] {
  const st = env.stats ?? {};
  return {
    factsExtracted: typeof st.facts_extracted === 'number' ? st.facts_extracted : factsLen,
    conditionsEvaluated: typeof st.conditions_evaluated === 'number' ? st.conditions_evaluated : part.evaluated,
    matched: typeof st.matched === 'number' ? st.matched : part.matched,
    unknown: typeof st.unknown === 'number' ? st.unknown : part.unknown,
  };
}

/** Normalize arbitrary API JSON into a UI view model */
export function normalizePipelineResponse(raw: unknown): UiPipelineViewModel {
  _id = 0;
  const env = (raw ?? {}) as McgPipelineApiEnvelope;
  const re =
    asRecord(env.rule_engine) ??
    asRecord(env.deterministic_rule_engine) ??
    asRecord(env.rule_engine_result);

  const factsArr = extractFactsArray(env);
  const facts = factsArr.map((r, i) => mapFactRow(r, i));

  const fullRouter = pickRankingsFull(env).map((c, i) => mapRouterRow(c, i));
  const strip = pickRouterList(env);
  const routerTopList = (strip.length ? strip : pickRankingsFull(env)).slice(0, 3).map((c, i) => mapRouterRow(c, i));

  const critRaw = gatherCriteria(env);
  const allCriteriaRows = critRaw.map(mapCriterionRow);
  const part = partitionCriteria(allCriteriaRows);

  const selected = pickSelectedModule(env, routerTopList.length ? routerTopList : fullRouter);

  return {
    selectedModule: selected,
    raw,
    facts,
    routerTop: routerTopList,
    routerFull: fullRouter.length ? fullRouter : routerTopList,
    admission: normalizeAdmission(raw, re),
    admissionRationale: rationaleFrom(env, re),
    topMatchedCriteria: part.matched,
    notableUnknownCriteria: part.unknowns,
    allCriteriaRows,
    revisedHpi:
      safeStr(env.revised_hpi) ?? safeStr(env.revised_hpi_note) ?? safeStr(env.optional_revised_hpi),
    trace: Array.isArray(env.trace) ? env.trace : undefined,
    stats: statsFrom(env, facts.length, part.stats),
    debugPayload: raw,
  };
}
