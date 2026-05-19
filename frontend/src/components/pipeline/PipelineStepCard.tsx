import type { ReactNode } from 'react';

type Props = {
  step: number;
  title: string;
  children: ReactNode;
};

export function PipelineStepCard({ step, title, children }: Props) {
  return (
    <section className="pr-step" aria-labelledby={`pr-step-${step}-title`}>
      <header className="pr-step__head">
        <span className="pr-step__index" aria-hidden="true">
          {String(step).padStart(2, '0')}
        </span>
        <h2 className="pr-step__title" id={`pr-step-${step}-title`}>
          <span className="pr-step__title-text">{title}</span>
        </h2>
      </header>
      <div className="pr-step__body">{children}</div>
    </section>
  );
}
