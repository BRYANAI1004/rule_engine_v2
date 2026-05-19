import type { UiFactRow } from '../../lib/pipelineTypes';

type Props = {
  rows: UiFactRow[];
};

export function NormalizedFactsTable({ rows }: Props) {
  if (!rows.length) {
    return (
      <div className="pr-el-empty" role="status">
        <span className="pr-el-empty__glyph" aria-hidden />
        <p className="pr-el-empty__title">No normalized clinical facts loaded</p>
        <p className="pr-el-empty__hint">Run extraction to populate this table — rows appear when the backend returns normalized fact records.</p>
      </div>
    );
  }

  return (
    <div className="pr-table-wrap">
      <table className="pr-table">
        <thead>
          <tr>
            <th scope="col">Fact</th>
            <th scope="col">Normalized condition key</th>
            <th scope="col">Value / measurement</th>
            <th scope="col">Evidence</th>
            <th scope="col">Source</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td>{r.factText}</td>
              <td>
                {r.mapped && r.conditionKey ? (
                  <code className="pr-code-pill">{r.conditionKey}</code>
                ) : (
                  <span className="pr-ghost">not mapped</span>
                )}
              </td>
              <td>{r.value ?? '—'}</td>
              <td className="pr-wrap">{r.evidence ?? '—'}</td>
              <td>{r.source ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
