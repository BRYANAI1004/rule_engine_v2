/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Full POST URL for MCG pipeline run, e.g. https://api.example/v1/mcg/pipeline/run */
  readonly VITE_MCG_PIPELINE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
