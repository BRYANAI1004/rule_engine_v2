import cors from 'cors';
import express from 'express';

import './lib/env.js';


const PORT = Number.parseInt(process.env.PORT ?? '4000', 10);
if (Number.isNaN(PORT)) {
  throw new Error(`Invalid PORT: ${process.env.PORT ?? ''}`);
}

const app = express();
app.use(cors());

app.get('/health', (_req, res) => {
  res.json({
    ok: true,
    service: 'ruleengine-v2-backend',
    timestamp: new Date().toISOString(),
  });
});

app.listen(PORT, () => {
  console.log(`ruleengine-v2-backend listening on http://127.0.0.1:${PORT}`);
});
