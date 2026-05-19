import { useMemo, type ReactNode } from 'react';

import type { UiAdmission, UiCriterionRow } from '../../lib/pipelineTypes';

type Props = {
  moduleLabel: string;
  admission: UiAdmission;
  rationale?: string;
  matched: UiCriterionRow[];
  unknownPreview: UiCriterionRow[];
  allRows: UiCriterionRow[];
};

function AdmissionBadge({ value }: { value: UiAdmission }) {
  const cls =
    value === 'YES' ? 'pr-badge pr-badge--yes' : value === 'NO' ? 'pr-badge pr-badge--no' : 'pr-badge pr-badge--unk';
  return (
    <span className={cls} role="status">
      {value}
    </span>
  );
}

function LogicPreview({ node }: { node: unknown }): ReactNode {
  if (node === null || node === undefined) {
    return <span className="pr-muted">null</span>;
  }
  if (typeof node === 'string' || typeof node === 'number' || typeof node === 'boolean') {
    return <span>{String(node)}</span>;
  }
  if (Array.isArray(node)) {
    return (
      <ul className="pr-logic-list">
        {node.map((n, i) => (
          <li key={i}>
            <LogicPreview node={n} />
          </li>
        ))}
      </ul>
    );
  }
  if (typeof node === 'object') {
    const o = node as Record<string, unknown>;
    const op = o.operator ?? o.op ?? o.type ?? o.kind;
    const opStr = typeof op === 'string' ? op.toUpperCase() : '';
    if (opStr === 'AND' || opStr === 'OR' || opStr === 'NOT') {
      const children = o.children ?? o.args ?? o.operands;
      return (
        <span className="pr-logic-node">
          <strong className="pr-op">{opStr}</strong>
          <span className="pr-logic-children">
            <LogicPreview node={children} />
          </span>
        </span>
      );
    }
  }
  return (
    <pre className="pr-pre pr-pre--tight" role="textbox">
      {JSON.stringify(node, null, 2)}
    </pre>
  );
}

function ExpandableCriterionRow({ row }: { row: UiCriterionRow }) {
  return (
    <details className="pr-criterion">
      <summary className="pr-criterion__summary">
        <span className="pr-crit-grid">
          <span className="pr-crit-grid__cell pr-crit-grid__fact">{row.label}</span>
          <span className="pr-crit-grid__cell pr-crit-grid__mono" data-res={row.result.toUpperCase()}>
            {row.result}
          </span>
          <span className="pr-crit-grid__cell pr-crit-grid__ev">{row.evidence ?? '—'}</span>
          <span className="pr-crit-grid__cell pr-crit-grid__logic">{row.logicSummary ?? '—'}</span>
        </span>
      </summary>
      <div className="pr-criterion__body">
        {row.logicTree !== undefined && row.logicTree !== null ? (
          <div className="pr-criterion__block">
            <div className="pr-label">Human-readable logic</div>
            <div className="pr-logic-box">
              <LogicPreview node={row.logicTree} />
            </div>
          </div>
        ) : null}
        {row.sourceCriteriaText ? (
          <div className="pr-criterion__block">
            <div className="pr-label">Source criteria text</div>
            <p className="pr-quote">{row.sourceCriteriaText}</p>
          </div>
        ) : null}
        <details className="pr-disclosure pr-disclosure--deep">
          <summary className="pr-disclosure__summary">
            <span className="pr-disclosure__chevron" aria-hidden />
            <span className="pr-disclosure__label">Runtime rule JSON</span>
          </summary>
          <pre className="pr-pre">{JSON.stringify(row.ruleJson ?? {}, null, 2)}</pre>
        </details>
      </div>
    </details>
  );
}

export function RuleEngineResultPanel({ moduleLabel, admission, rationale, matched, unknownPreview, allRows }: Props) {
  const debugRows = useMemo(() => {
    const shown = new Set(matched.map((r) => r.id));
    unknownPreview.forEach((u) => shown.add(u.id));
    return allRows.filter((r) => !shown.has(r.id));
  }, [allRows, matched, unknownPreview]);

  return (
    <div className="pr-rule">
      <div className="pr-rule__module">
        <span className="pr-label">Selected module</span>
        <h3 className="pr-rule__title">{moduleLabel}</h3>
      </div>

      <div className="pr-rule__admit">
        <div className="pr-rule__admit-row">
          <span className="pr-label">Admission recommendation (deterministic engine)</span>
          <AdmissionBadge value={admission} />
        </div>
        {rationale ? <p className="pr-rule__rationale">{rationale}</p> : null}
        <p className="pr-microcopy">
          Recommendation is produced by the deterministic rule engine over normalized facts. It is independent of any
          optional revised narrative (step 5).
        </p>
      </div>

      <h4 className="pr-subhead">Top matched admission criteria</h4>
      {matched.length === 0 ? (
        <div className="pr-el-empty" role="status">
          <span className="pr-el-empty__glyph" aria-hidden />
          <p className="pr-el-empty__title">No primary TRUE / MATCHED rows</p>
          <p className="pr-el-empty__hint">
            The rule engine surfaced no admission-positive criteria at the top tier. Expand the evaluation table below to scan the full deterministic output.
          </p>
        </div>
      ) : (
        <div className="pr-crit-shell">
          <div className="pr-crit-grid pr-crit-grid--head" aria-hidden="true">
            <span>Criterion / condition</span>
            <span>Result</span>
            <span>Evidence</span>
            <span>Rule logic summary</span>
          </div>
          {matched.map((row) => (
            <ExpandableCriterionRow key={row.id} row={row} />
          ))}
        </div>
      )}

      {unknownPreview.length > 0 ? (
        <details className="pr-disclosure pr-disclosure--tight">
          <summary className="pr-disclosure__summary">
            <span className="pr-disclosure__chevron" aria-hidden />
            <span className="pr-disclosure__label">Other conditions with unknown evaluation (limited)</span>
          </summary>
          <div className="pr-disclosure__body">
            <ul className="pr-mini-list">
              {unknownPreview.map((u) => (
                <li key={u.id}>
                  <strong>{u.label}</strong> — {u.result}
                  {u.evidence ? <span className="pr-muted"> · {u.evidence}</span> : null}
                </li>
              ))}
            </ul>
          </div>
        </details>
      ) : null}

      <details className="pr-disclosure pr-disclosure--tight">
        <summary className="pr-disclosure__summary">
          <span className="pr-disclosure__chevron" aria-hidden />
          <span className="pr-disclosure__label">Full evaluation table (debug)</span>
        </summary>
        <div className="pr-disclosure__body">
          {debugRows.length === 0 ? (
            <p className="pr-muted">No additional rows beyond those shown above.</p>
          ) : (
            <div className="pr-table-wrap">
              <table className="pr-table pr-table--compact">
                <thead>
                  <tr>
                    <th>Criterion</th>
                    <th>Result</th>
                    <th>Evidence</th>
                    <th>Internal</th>
                  </tr>
                </thead>
                <tbody>
                  {debugRows.map((r) => (
                    <tr key={r.id}>
                      <td>{r.label}</td>
                      <td>{r.result}</td>
                      <td className="pr-wrap">{r.evidence ?? '—'}</td>
                      <td className="pr-wrap">{r.isNoise ? 'filtered from primary view' : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </details>
    </div>
  );
}
