/**
 * Placeholder types for future MCG HTML → source tree ingestion.
 * Aligns with the planned JSON shape: document → sections → nodes, plus tables,
 * footnotes, references, extracted codes, and parser warnings.
 */

export interface McgSourceDocument {
  source_document: {
    id: string;
    title?: string;
    /** Stable fingerprint of raw snapshot bytes or normalized HTML text */
    content_sha256?: string;
    captured_at?: string;
  };
  sections: McgSourceSection[];
  source_nodes: McgSourceNode[];
  tables: McgSourceTable[];
  footnotes: McgFootnote[];
  references: McgReference[];
  /** ICD/CPT/other codes surfaced from the document for downstream mapping */
  codes: string[];
  warnings: string[];
}

export interface McgSourceSection {
  id: string;
  heading?: string;
  /** Ordering within the document tree */
  ordinal?: number;
  parent_section_id?: string;
}

export interface McgSourceNode {
  id: string;
  section_id?: string;
  /** Paragraph, list item, heading fragment, etc. */
  kind?: string;
  text?: string;
  ordinal?: number;
}

export interface McgSourceTable {
  id: string;
  section_id?: string;
  caption?: string;
  /** Row-major cells or structured rows — refined in later steps */
  rows?: unknown[];
}

export interface McgFootnote {
  id: string;
  marker?: string;
  text?: string;
}

export interface McgReference {
  id: string;
  citation_text?: string;
  uri?: string;
}

/** Versioned bundle produced by ingestion + validation before staging */
export interface McgSourcePack {
  pack_id: string;
  generated_at: string;
  document: McgSourceDocument;
}
