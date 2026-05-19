import { NavLink } from 'react-router-dom';

import { SiteNav } from '../components/SiteNav';

import '../styles/pipeline-review.css';

export function HomePage() {
  const steps = [
    'Step 0 project initialized',
    'Step 1 MCG HTML capture',
    'Step 2 source-tree JSON extraction',
    'Step 3 Supabase staging',
  ] as const;

  return (
    <div className="pr-page pr-page--minimal" id="main-content">
      <SiteNav />

      <div className="pr-surface pr-surface--narrow">
        <header className="pr-hero pr-hero--compact">
          <h1 className="pr-hero__title">MCG rule engine v2</h1>
          <p className="pr-hero__tagline">Source ingestion · clinical pipeline workspace</p>
          <p className="pr-hero__lede shell-home__lede">
            Warm staging environment for ingestion artifacts; open the deterministic pipeline cockpit for audited case execution.
          </p>
          <NavLink className="pr-btn pr-btn--primary shell-home__cta" to="/rule-engine/pipeline-review">
            Open pipeline review
          </NavLink>
        </header>

        <section className="pr-card shell-home-card" aria-label="workspace checklist">
          <div className="pr-card__head pr-card__head--tight">
            <h2 className="pr-card__heading shell-home-card__title">workspace checklist</h2>
          </div>
          <div className="pr-card__body">
            <ul className="shell-home__list">
              {steps.map((label) => (
                <li key={label}>{label}</li>
              ))}
            </ul>
          </div>
        </section>
      </div>
    </div>
  );
}
