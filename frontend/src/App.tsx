export function App() {
  const steps = [
    'Step 0 project initialized',
    'Step 1 MCG HTML capture',
    'Step 2 source-tree JSON extraction',
    'Step 3 Supabase staging',
  ] as const;

  return (
    <main className="app">
      <h1 className="title">MCG Rule Engine v2</h1>
      <p className="subtitle">Source ingestion pipeline placeholder</p>

      <ul className="checklist">
        {steps.map((label) => (
          <li key={label}>{label}</li>
        ))}
      </ul>
    </main>
  );
}
