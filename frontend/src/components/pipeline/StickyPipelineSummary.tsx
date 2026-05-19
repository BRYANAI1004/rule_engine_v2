import type { UiAdmission, UiCriterionRow, UiPipelineViewModel, UiRouterRow } from '../../lib/pipelineTypes';

type Props = {
  view: UiPipelineViewModel | null;
  topRoute: UiRouterRow | null;
};

function LabelAdmission({ v }: { v: UiAdmission }) {
  const cls = v === 'YES' ? 'pr-sum__val pr-sum__val--yes' : v === 'NO' ? 'pr-sum__val' : 'pr-sum__val pr-sum__val--unk';
  return <span className={cls}>{v}</span>;
}

export function StickyPipelineSummary({ view, topRoute }: Props) {
  const crit = view?.topMatchedCriteria ?? [];
  const shortLines = crit.slice(0, 3).map((c: UiCriterionRow) => c.label);

  return (
    <aside className="pr-aside" aria-label="Case summary">
      <div className="pr-aside__inner">
        <h2 className="pr-aside__title">Case summary</h2>

        <dl className="pr-sum">
          <div>
            <dt>Admission</dt>
            <dd>{view ? <LabelAdmission v={view.admission} /> : <span className="pr-muted">—</span>}</dd>
          </div>
          <div>
            <dt>Selected MCG</dt>
            <dd>
              {view?.selectedModule ? (
                <>
                  <strong>{view.selectedModule.code}</strong>
                  {view.selectedModule.title ? (
                    <span className="pr-muted">
                      <br />
                      {view.selectedModule.title}
                    </span>
                  ) : null}
                </>
              ) : (
                <span className="pr-muted">No module selected</span>
              )}
            </dd>
          </div>
          <div>
            <dt>Top route score</dt>
            <dd>
              {topRoute?.score !== undefined ? (
                <>
                  {topRoute.score}
                  {topRoute.strength ? <span className="pr-muted"> · {topRoute.strength}</span> : null}
                </>
              ) : (
                <span className="pr-muted">—</span>
              )}
            </dd>
          </div>
          <div>
            <dt>Rule engine</dt>
            <dd>Deterministic</dd>
          </div>
        </dl>

        <h3 className="pr-aside__sub">Top matched criteria</h3>
        {shortLines.length ? (
          <ol className="pr-aside__list">
            {shortLines.map((line, i) => (
              <li key={i}>{line}</li>
            ))}
          </ol>
        ) : (
          <p className="pr-muted pr-aside__muted">Run the pipeline to populate.</p>
        )}

        <h3 className="pr-aside__sub">Evidence coverage</h3>
        <ul className="pr-coverage">
          <li>
            Facts extracted: <strong>{view?.stats.factsExtracted ?? '—'}</strong>
          </li>
          <li>
            Conditions evaluated: <strong>{view?.stats.conditionsEvaluated ?? '—'}</strong>
          </li>
          <li>
            Matched: <strong>{view?.stats.matched ?? '—'}</strong>
          </li>
          <li>
            Unknown: <strong>{view?.stats.unknown ?? '—'}</strong>
          </li>
        </ul>

        <details className="pr-disclosure pr-disclosure--aside">
          <summary className="pr-disclosure__summary">
            <span className="pr-disclosure__chevron" aria-hidden />
            <span className="pr-disclosure__label">Debug</span>
            <span className="pr-disclosure__hint">Compact raw payload</span>
          </summary>
          <p className="pr-microcopy">Raw response JSON for troubleshooting integrations.</p>
          <pre className="pr-pre pr-pre--small">{view ? JSON.stringify(view.raw, null, 2) : '—'}</pre>
        </details>
      </div>
    </aside>
  );
}
