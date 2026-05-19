import { BrowserRouter, Route, Routes } from 'react-router-dom';

import { HomePage } from './pages/HomePage';
import { PipelineReviewPage } from './pages/PipelineReviewPage';

export function App() {
  return (
    <BrowserRouter>
      <div className="app-shell">
        <a href="#main-content" className="skip-link">
          Skip to content
        </a>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/rule-engine" element={<HomePage />} />
          <Route path="/rule-engine/pipeline-review" element={<PipelineReviewPage />} />
        </Routes>
      </div>
    </BrowserRouter>
  );
}
