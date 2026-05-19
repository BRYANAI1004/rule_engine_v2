import { NavLink } from 'react-router-dom';

/**
 * Workspace navigation — purely presentational routing; no pipeline logic.
 */
export function SiteNav() {
  return (
    <nav className="pr-site-nav" aria-label="Workspace">
      <div className="pr-site-nav__track">
        <NavLink
          to="/rule-engine/pipeline-review"
          className={({ isActive }) =>
            ['pr-site-nav__pill', isActive ? 'pr-site-nav__pill--active' : ''].join(' ')
          }
        >
          MCG pipeline
        </NavLink>
        <span className="pr-site-nav__pill pr-site-nav__pill--muted" title="Not available in this workspace">
          Dictionary
        </span>
      </div>
    </nav>
  );
}
