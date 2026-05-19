import type { UiRouterRow } from '../../lib/pipelineTypes';

type Props = {
  top: UiRouterRow[];
  full: UiRouterRow[];
};

export function RouterCandidatesPanel({ top, full }: Props) {
  const showFull = full.length > top.length;

  return (
    <div className="pr-router">
      <p className="pr-microcopy">
        Deterministic keyword routing. Scores reflect lexical overlap against module routing strips — not an LLM module
        picker.
      </p>
      {top.length === 0 ? (
        <div className="pr-el-empty" role="status">
          <span className="pr-el-empty__glyph" aria-hidden />
          <p className="pr-el-empty__title">No routing candidates surfaced</p>
          <p className="pr-el-empty__hint">
            Routing results appear once the deterministic ranker responds. If blank, inspect the upstream router payload — not UI rendering.
          </p>
        </div>
      ) : (
        <div className="pr-table-wrap">
          <table className="pr-table pr-table--compact">
            <thead>
              <tr>
                <th scope="col">MCG</th>
                <th scope="col">Title</th>
                <th scope="col">Score</th>
                <th scope="col">Matched routing signals</th>
              </tr>
            </thead>
            <tbody>
              {top.map((r) => (
                <tr key={r.id}>
                  <td>
                    <strong>{r.mcgCode}</strong>
                  </td>
                  <td>{r.title ?? '—'}</td>
                  <td>
                    {r.score !== undefined ? r.score : '—'}
                    {r.strength ? <span className="pr-muted"> · {r.strength}</span> : null}
                  </td>
                  <td className="pr-wrap pr-router__signals">{r.signalsInline || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showFull ? (
        <details className="pr-disclosure pr-disclosure--tight">
          <summary className="pr-disclosure__summary">
            <span className="pr-disclosure__chevron" aria-hidden />
            <span className="pr-disclosure__label">View full ranking</span>
          </summary>
          <div className="pr-disclosure__body">
            <div className="pr-table-wrap">
              <table className="pr-table pr-table--compact">
                <thead>
                  <tr>
                    <th scope="col">Rank</th>
                    <th scope="col">MCG</th>
                    <th scope="col">Title</th>
                    <th scope="col">Score</th>
                    <th scope="col">Signals</th>
                  </tr>
                </thead>
                <tbody>
                  {full.map((r, idx) => (
                    <tr key={`${r.id}-full-${idx}`}>
                      <td>{idx + 1}</td>
                      <td>{r.mcgCode}</td>
                      <td>{r.title ?? '—'}</td>
                      <td>{r.score !== undefined ? r.score : '—'}</td>
                      <td className="pr-wrap">{r.signalsInline || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </details>
      ) : null}
    </div>
  );
}
