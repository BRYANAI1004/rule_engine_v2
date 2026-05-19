type StageStatus = 'pending' | 'active' | 'done' | 'skipped';

export type PipelineStage = {
  id: string;
  label: string;
  status: StageStatus;
};

type Props = {
  stages: PipelineStage[];
  busy: boolean;
};

/**
 * Horizontal pipeline UX rail — derives only from props; status is computed by the parent page.
 */
export function PipelineStageRail({ stages, busy }: Props) {
  return (
    <div className="pr-stage-rail" aria-busy={busy}>
      <p className="pr-stage-rail__label">Clinical pipeline stages</p>
      <ol className="pr-stage-rail__list">
        {stages.map((s) => (
          <li key={s.id} className="pr-stage-rail__step">
            <span
              className={['pr-stage-rail__dot', `pr-stage-rail__dot--${s.status}`].join(' ')}
              aria-hidden="true"
            />
            <span className="pr-stage-rail__text">{s.label}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}
