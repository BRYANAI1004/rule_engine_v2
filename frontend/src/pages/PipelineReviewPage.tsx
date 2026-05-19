import { useCallback, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

import { SiteNav } from '../components/SiteNav';
import { PipelineStepCard } from '../components/pipeline/PipelineStepCard';
import { PipelineStageRail, type PipelineStage } from '../components/pipeline/PipelineStageRail';
import { RevisedHpiPanel } from '../components/pipeline/RevisedHpiPanel';
import { NormalizedFactsTable } from '../components/pipeline/NormalizedFactsTable';
import { RouterCandidatesPanel } from '../components/pipeline/RouterCandidatesPanel';
import { RuleEngineResultPanel } from '../components/pipeline/RuleEngineResultPanel';
import { SourceNotesPanel } from '../components/pipeline/SourceNotesPanel';
import { StickyPipelineSummary } from '../components/pipeline/StickyPipelineSummary';
import { resolvePipelineEndpoint, runMcgPipeline } from '../lib/mcgPipelineApi';
import { PIPELINE_SAMPLE_CASE } from '../lib/sampleCase';
import type { UiPipelineViewModel } from '../lib/pipelineTypes';

import '../styles/pipeline-review.css';

type ErrorState = { message: string; detail?: unknown };

function formatPipelineEndpoint(ep: string): string {
  if (ep.startsWith('/')) return `Same-origin · ${ep}`;
  try {
    const u = new URL(ep);
    const path = u.pathname.replace(/\/$/, '');
    const suffix = path && path !== '/' ? (path.length > 40 ? `${path.slice(0, 38)}…` : path) : '';
    return `${u.host}${suffix ? ` · ${suffix}` : ''}`;
  } catch {
    return ep.length > 52 ? `${ep.slice(0, 50)}…` : ep;
  }
}

export function PipelineReviewPage() {
  const [originalHpi, setOriginalHpi] = useState('');
  const [erNote, setErNote] = useState('');
  const [moduleOverride, setModuleOverride] = useState('');
  const [view, setView] = useState<UiPipelineViewModel | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ErrorState | null>(null);

  const endpointLabel = useMemo(() => formatPipelineEndpoint(resolvePipelineEndpoint()), []);

  const pipelineStages = useMemo((): PipelineStage[] => {
    const baseLabels: Omit<PipelineStage, 'status'>[] = [
      { id: 'sn', label: 'Source notes' },
      { id: 'facts', label: 'Fact extraction' },
      { id: 'route', label: 'Routing' },
      { id: 'rule', label: 'Rule engine' },
      { id: 'hpi', label: 'Revised HPI' },
    ];

    if (busy) {
      return baseLabels.map((s, i) => ({
        ...s,
        status: i === 0 ? 'done' : i === 1 ? 'active' : ('pending' as const),
      }));
    }

    if (view) {
      const hasRevised = Boolean(view.revisedHpi?.trim());
      return baseLabels.map((s, i) => ({
        ...s,
        status: i < 4 ? 'done' : hasRevised ? 'done' : ('skipped' as const),
      }));
    }

    return baseLabels.map((s) => ({ ...s, status: 'pending' as const }));
  }, [busy, view]);

  const topRoute = view?.routerTop?.[0] ?? null;

  const moduleLabel = useMemo(() => {
    if (!view?.selectedModule) return 'No module selected';
    const { code, title } = view.selectedModule;
    return title ? `${code} · ${title}` : code;
  }, [view]);

  const breadcrumbPieces = useMemo(() => {
    if (busy) return ['Submission received', 'Extraction'];
    if (view?.selectedModule) {
      const code = view.selectedModule.code;
      return [`Module ${code}`, view.admission];
    }
    return ['Awaiting submission', '—'];
  }, [busy, view]);

  const onRun = useCallback(async () => {
    setBusy(true);
    setError(null);
    setView(null);
    try {
      const res = await runMcgPipeline({
        original_hpi: originalHpi,
        er_note: erNote,
        selected_mcg_code: moduleOverride.trim() || undefined,
      });
      if (!res.ok) {
        setError({ message: 'Pipeline run failed', detail: res });
        return;
      }
      setView(res.view);
    } catch (e) {
      setError({ message: 'Pipeline run failed', detail: e });
    } finally {
      setBusy(false);
    }
  }, [erNote, moduleOverride, originalHpi]);

  const onClear = useCallback(() => {
    setOriginalHpi('');
    setErNote('');
    setModuleOverride('');
    setView(null);
    setError(null);
  }, []);

  const onSample = useCallback(() => {
    setOriginalHpi(PIPELINE_SAMPLE_CASE.original_hpi);
    setErNote(PIPELINE_SAMPLE_CASE.er_note);
    setView(null);
    setError(null);
  }, []);

  return (
    <div className="pr-page" id="main-content">
      <SiteNav />

      <div className="pr-surface">
        <header className="pr-hero">
          <div className="pr-hero__row">
            <div className="pr-hero__main">
              <p className="pr-hero__kicker">
                <Link to="/" className="pr-hero__back">
                  ← Workspace
                </Link>
              </p>
              <h1 className="pr-hero__title">MCG rule engine</h1>
              <p className="pr-hero__tagline">Deterministic clinical pipeline</p>
            </div>
            <div className="pr-hero__stripe" aria-hidden="true" />
          </div>
          <p className="pr-hero__lede">
            Source notes · normalized facts · deterministic routing · rule engine · optional revised HPI
          </p>
          <dl className="pr-hero__meta">
            <div className="pr-hero__meta-item">
              <dt>Pipeline API</dt>
              <dd>
                <code className="pr-mono-soft">{endpointLabel}</code>
              </dd>
            </div>
            <div className="pr-hero__meta-item">
              <dt>Provider · model scope</dt>
              <dd>
                Backend-configured normalization · <span className="pr-hero__meta-strong">no runtime LLM admission</span>
              </dd>
            </div>
          </dl>

          <div className="pr-hero__breadcrumb" aria-label="Latest pipeline posture">
            {breadcrumbPieces.map((piece, idx) => (
              <span key={`${piece}-${idx}`} className="pr-hero__crumb">
                {piece}
                {idx < breadcrumbPieces.length - 1 ? (
                  <span className="pr-hero__crumb-sep" aria-hidden="true">
                    /
                  </span>
                ) : null}
              </span>
            ))}
          </div>

          <PipelineStageRail stages={pipelineStages} busy={busy} />
        </header>

        <section className="pr-card pr-card--input" aria-labelledby="pr-input-heading">
          <div className="pr-card__head">
            <h2 id="pr-input-heading" className="pr-card__heading">
              Case input
            </h2>
            <p className="pr-card__deck">Admission remains deterministic; excerpts are used only for extraction and routing.</p>
          </div>
          <div className="pr-card__body pr-card__body--flush-y">
            <div className="pr-editor-grid">
              <label className="pr-field">
                <span className="pr-field__label">Original HPI</span>
                <textarea
                  className="pr-textarea pr-textarea--editor"
                  value={originalHpi}
                  onChange={(e) => setOriginalHpi(e.target.value)}
                  rows={9}
                  autoComplete="off"
                  spellCheck={false}
                  placeholder="Chief complaint, onset, course, relevant meds — paste clinical narrative."
                />
              </label>
              <label className="pr-field">
                <span className="pr-field__label">ER note</span>
                <textarea
                  className="pr-textarea pr-textarea--editor"
                  value={erNote}
                  onChange={(e) => setErNote(e.target.value)}
                  rows={9}
                  autoComplete="off"
                  spellCheck={false}
                  placeholder="ED assessment, diagnostics, bedside findings — corroborating source."
                />
              </label>
            </div>

            <details className="pr-advanced">
              <summary className="pr-advanced__summary">
                <span className="pr-advanced__chevron" aria-hidden />
                <span className="pr-advanced__title">Advanced options</span>
                <span className="pr-advanced__hint">Override routing when your API supports explicit module hints.</span>
              </summary>
              <div className="pr-advanced__panel">
                <label className="pr-field">
                  <span className="pr-field__label">Optional MCG override (if API supports it)</span>
                  <input
                    className="pr-input"
                    value={moduleOverride}
                    onChange={(e) => setModuleOverride(e.target.value)}
                    placeholder="e.g. M282"
                    autoComplete="off"
                  />
                </label>
              </div>
            </details>

            <div className="pr-actions">
              <button type="button" className="pr-btn pr-btn--primary" onClick={onRun} disabled={busy}>
                {busy ? 'Running…' : 'Run pipeline'}
              </button>
              <button type="button" className="pr-btn pr-btn--ghost" onClick={onClear} disabled={busy}>
                Clear
              </button>
              <button type="button" className="pr-btn pr-btn--outline" onClick={onSample} disabled={busy}>
                Load sample case
              </button>
            </div>
          </div>
        </section>

        {busy ? (
          <div className="pr-loading" role="status" aria-live="polite">
            <div className="pr-loading__bar" />
            <p>Running pipeline — extraction, deterministic routing, and rule evaluation.</p>
          </div>
        ) : null}

        {error ? (
          <section className="pr-error" aria-live="assertive">
            <h2 className="pr-error__title">{error.message}</h2>
            <p className="pr-error__msg">
              {typeof error.detail === 'object' && error.detail !== null && 'message' in error.detail
                ? String((error.detail as { message?: unknown }).message)
                : 'Check your network connection and API endpoint configuration.'}
            </p>
            <details className="pr-disclosure">
              <summary className="pr-disclosure__summary">
                <span className="pr-disclosure__chevron" aria-hidden />
                <span className="pr-disclosure__label">Technical detail</span>
                <span className="pr-disclosure__hint">Raw error payload.</span>
              </summary>
              <pre className="pr-pre">{JSON.stringify(error.detail ?? {}, null, 2)}</pre>
            </details>
          </section>
        ) : null}

        <div className="pr-layout">
          <div className="pr-main">
            {!view && !busy && !error ? (
              <div className="pr-el-empty pr-el-empty--prime" role="status">
                <span className="pr-el-empty__glyph" aria-hidden />
                <p className="pr-el-empty__title">Ready for clinical audit intake</p>
                <p className="pr-el-empty__hint">
                  Run the pipeline to populate downstream stages. Configure <code className="pr-mono-soft">VITE_MCG_PIPELINE_URL</code>{' '}
                  to point at your service, or proxy <code className="pr-mono-soft">/api/mcg/pipeline/run</code>.
                </p>
              </div>
            ) : null}

            {view ? (
              <div className="pr-results">
                <PipelineStepCard step={1} title="Source notes">
                  <SourceNotesPanel originalHpi={originalHpi} erNote={erNote} />
                </PipelineStepCard>

                <PipelineStepCard step={2} title="LLM extracted & normalized facts">
                  <p className="pr-microcopy">
                    Facts are extracted and normalized for matching only. They feed deterministic routing and the rule
                    engine.
                  </p>
                  <NormalizedFactsTable rows={view.facts} />
                </PipelineStepCard>

                <PipelineStepCard step={3} title="Deterministic routing">
                  <RouterCandidatesPanel top={view.routerTop} full={view.routerFull} />
                </PipelineStepCard>

                <PipelineStepCard step={4} title="Deterministic rule engine result">
                  <RuleEngineResultPanel
                    moduleLabel={moduleLabel}
                    admission={view.admission}
                    rationale={view.admissionRationale}
                    matched={view.topMatchedCriteria}
                    unknownPreview={view.notableUnknownCriteria}
                    allRows={view.allCriteriaRows}
                  />
                  {view.trace?.length ? (
                    <details className="pr-disclosure pr-disclosure--tight">
                      <summary className="pr-disclosure__summary">
                        <span className="pr-disclosure__chevron" aria-hidden />
                        <span className="pr-disclosure__label">Execution trace (debug)</span>
                      </summary>
                      <pre className="pr-pre">{JSON.stringify(view.trace, null, 2)}</pre>
                    </details>
                  ) : null}
                </PipelineStepCard>

                <PipelineStepCard step={5} title="Optional revised HPI">
                  <RevisedHpiPanel text={view.revisedHpi} />
                </PipelineStepCard>
              </div>
            ) : null}
          </div>

          <StickyPipelineSummary view={view} topRoute={topRoute} />
        </div>
      </div>
    </div>
  );
}
