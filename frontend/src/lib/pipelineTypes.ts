/**
 * Tolerant types for MCG pipeline API responses.
 * Backends may vary field names; normalization lives in `normalizePipelineResponse`.
 */

export type Trileanish = 'YES' | 'NO' | 'UNKNOWN' | 'TRUE' | 'FALSE' | string;

/** Raw fact row — field names are best-effort across services */
export type PipelineFactRecord = {
  fact?: string;
  text?: string;
  normalized_fact?: string;
  normalized?: string;
  condition_key?: string;
  normalized_condition_key?: string;
  key?: string;
  value?: string;
  measurement?: string;
  evidence?: string;
  evidence_quote?: string;
  quote?: string;
  span?: string;
  source?: string;
  source_field?: string;
  mapped?: boolean;
  [key: string]: unknown;
};

export type PipelineRouterCandidate = {
  mcg_code?: string;
  code?: string;
  module_code?: string;
  title?: string;
  name?: string;
  score?: number;
  routing_score?: number;
  strength?: string;
  signal_strength?: string;
  matched_routing_signals?: string[];
  matched_signals?: string[];
  routing_signals?: string[];
  matched_terms?: string[];
  terms?: string[];
  [key: string]: unknown;
};

export type PipelineCriterionRow = {
  id?: string;
  criterion?: string;
  condition?: string;
  label?: string;
  name?: string;
  description?: string;
  result?: Trileanish;
  status?: string;
  matched?: boolean;
  trilean?: Trileanish;
  evidence?: string;
  evidence_quote?: string;
  rationale?: string;
  rule_logic_summary?: string;
  logic_summary?: string;
  logic_tree?: unknown;
  human_logic?: unknown;
  rule?: unknown;
  runtime_rule?: unknown;
  source_criteria_text?: string;
  criteria_source_text?: string;
  review_status?: string;
  internal_status?: string;
  [key: string]: unknown;
};

/** Loose envelope — any extra keys preserved in `raw` for debug */
export type McgPipelineApiEnvelope = {
  selected_mcg_code?: string;
  selected_module?: { code?: string; title?: string; name?: string };
  module?: { code?: string; title?: string };
  original_hpi?: string;
  er_note?: string;
  /** Facts keyed or listed */
  normalized_for_matching?: PipelineFactRecord[] | Record<string, unknown>;
  normalized_facts?: PipelineFactRecord[];
  extracted_facts?: PipelineFactRecord[];
  facts?: PipelineFactRecord[];
  llm_facts?: PipelineFactRecord[];
  router_candidates?: PipelineRouterCandidate[];
  router_candidate_strip?: PipelineRouterCandidate[];
  /** Full deterministic ranking list */
  router_rankings?: PipelineRouterCandidate[];
  rankings?: PipelineRouterCandidate[];
  full_router_rankings?: PipelineRouterCandidate[];
  rule_engine?: Record<string, unknown>;
  deterministic_rule_engine?: Record<string, unknown>;
  rule_engine_result?: Record<string, unknown>;
  admission_recommendation?: Trileanish | Record<string, unknown>;
  admission?: Trileanish;
  admission_decision?: Trileanish;
  evaluation_rows?: PipelineCriterionRow[];
  criteria_evaluations?: PipelineCriterionRow[];
  evaluations?: PipelineCriterionRow[];
  trace?: unknown[];
  revised_hpi?: string;
  revised_hpi_note?: string;
  optional_revised_hpi?: string;
  stats?: Record<string, number>;
  meta?: Record<string, unknown>;
  error?: string;
  message?: string;
  [key: string]: unknown;
};

export type UiAdmission = 'YES' | 'NO' | 'UNKNOWN';

export type UiFactRow = {
  id: string;
  factText: string;
  conditionKey?: string;
  mapped: boolean;
  value?: string;
  evidence?: string;
  source?: string;
};

export type UiRouterRow = {
  id: string;
  mcgCode: string;
  title?: string;
  score?: number;
  strength?: string;
  signalsInline: string;
};

export type UiCriterionRow = {
  id: string;
  label: string;
  result: string;
  evidence?: string;
  logicSummary?: string;
  logicTree?: unknown;
  ruleJson?: unknown;
  sourceCriteriaText?: string;
  isNoise?: boolean;
};

export type UiPipelineViewModel = {
  selectedModule: { code: string; title?: string } | null;
  raw: unknown;
  facts: UiFactRow[];
  routerTop: UiRouterRow[];
  routerFull: UiRouterRow[];
  admission: UiAdmission;
  admissionRationale?: string;
  /** Top matched (TRUE / YES) criteria for primary display */
  topMatchedCriteria: UiCriterionRow[];
  /** Small set of unknowns for secondary section */
  notableUnknownCriteria: UiCriterionRow[];
  allCriteriaRows: UiCriterionRow[];
  revisedHpi?: string;
  trace?: unknown[];
  stats: {
    factsExtracted: number;
    conditionsEvaluated: number;
    matched: number;
    unknown: number;
  };
  /** Copy for debug / collapsed panels */
  debugPayload: unknown;
};
