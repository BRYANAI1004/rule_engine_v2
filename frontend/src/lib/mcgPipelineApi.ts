import { normalizePipelineResponse } from './normalizePipelineResponse';
import type { UiPipelineViewModel } from './pipelineTypes';

export type RunPipelineInput = {
  original_hpi: string;
  er_note: string;
  /** When your service requires an explicit module override */
  selected_mcg_code?: string;
};

export type RunPipelineSuccess = {
  ok: true;
  view: UiPipelineViewModel;
};

export type RunPipelineFailure = {
  ok: false;
  message: string;
  status?: number;
  body?: unknown;
};

export type RunPipelineResult = RunPipelineSuccess | RunPipelineFailure;

/**
 * Endpoint resolution:
 * - `VITE_MCG_PIPELINE_URL` full URL to POST (e.g. https://api.example/v1/mcg/pipeline/run)
 * - If unset, defaults to same-origin `/api/mcg/pipeline/run` (often paired with a dev proxy).
 */
export function resolvePipelineEndpoint(): string {
  const fromEnv = import.meta.env.VITE_MCG_PIPELINE_URL?.trim();
  if (fromEnv) return fromEnv;
  return '/api/mcg/pipeline/run';
}

export async function runMcgPipeline(input: RunPipelineInput): Promise<RunPipelineResult> {
  const url = resolvePipelineEndpoint();
  let res: Response;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({
        original_hpi: input.original_hpi,
        er_note: input.er_note,
        selected_mcg_code: input.selected_mcg_code,
      }),
    });
  } catch (e) {
    const msg = e instanceof Error ? e.message : 'Network error';
    return { ok: false, message: msg };
  }

  const text = await res.text();
  let json: unknown = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = { _parse_error: text };
  }

  if (!res.ok) {
    const message =
      typeof json === 'object' && json !== null && 'message' in json
        ? String((json as { message?: unknown }).message ?? res.statusText)
        : res.statusText || 'Request failed';
    return { ok: false, message, status: res.status, body: json };
  }

  const unwrapped =
    json && typeof json === 'object' && 'data' in json && (json as { data: unknown }).data !== undefined
      ? (json as { data: unknown }).data
      : json;

  const view = normalizePipelineResponse(unwrapped);
  return { ok: true, view };
}
