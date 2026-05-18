"""Shared typing helpers for MCG source tree (schema mcg_source_tree.v1)."""

from __future__ import annotations

from typing import Any, Literal, TypedDict


class LogicHint(TypedDict, total=False):
    raw_phrase: str
    inferred_operator: str
    confidence: Literal["high", "medium", "low"]
    hint_kind: str


class SourceNode(TypedDict, total=False):
    source_node_id: str
    mcg_code: str
    section_id: str
    domain: str
    parent_source_node_id: str | None
    source_depth: int
    sort_order: int
    node_type: str
    original_text: str
    normalized_text: str
    html_tag: str
    html_anchor: str | None
    html_path: str
    collapse_id: str | None
    expanded_div_id: str | None
    collapsed_div_id: str | None
    expand_group_parent: str | None
    expanded: bool
    logic_hint: LogicHint | None
    footnote_refs: list[str]
    reference_refs: list[str]
    text_hash: str
    warnings: list[str]


class SectionRecord(TypedDict, total=False):
    section_id: str
    mcg_code: str
    section_key: str
    domain: str
    title: str
    html_anchor: str | None
    html_tag: str
    logictype: str | None
    data_role: str | None
    sort_order: int
    text_hash: str


class TableRow(TypedDict):
    row_index: int
    cells: dict[str, str | list[str]]


class TableRecord(TypedDict, total=False):
    table_id: str
    mcg_code: str
    section_id: str
    domain: str
    title: str
    columns: list[str]
    rows: list[TableRow]
    source_ref_ids: list[str]
    text_hash: str


class FootnoteRecord(TypedDict, total=False):
    footnote_id: str
    mcg_code: str
    footnote_key: str
    text: str
    reference_refs: list[str]
    text_hash: str


class ReferenceRecord(TypedDict, total=False):
    reference_id: str
    mcg_code: str
    reference_number: str
    text: str
    doi: str | None
    context_links: list[str]
    text_hash: str


class CodeGroupRecord(TypedDict, total=False):
    code_group_id: str
    mcg_code: str
    code_system: str
    codes: list[str]
    descriptions: list[str]
    text_hash: str


class CollapseExpandMap(TypedDict):
    raw: str
    parsed: dict[str, list[str]]


class SourceTreeV1(TypedDict, total=False):
    schema_version: str
    source_document: dict[str, Any]
    sections: list[SectionRecord]
    source_nodes: list[SourceNode]
    tables: list[TableRecord]
    footnotes: list[FootnoteRecord]
    references: list[ReferenceRecord]
    codes: list[CodeGroupRecord]
    collapse_expand_map: CollapseExpandMap
    audit: dict[str, Any]


class AdmissionAudit(TypedDict, total=False):
    found: bool
    root_found: bool
    root_logic_hint: str | None
    expected_path_texts_found: dict[str, bool]


class DischargeAudit(TypedDict, total=False):
    found: bool
    planning_found: bool
    destination_found: bool
    patient_safe_to_go_home_found: bool
    medication_reconciliation_found: bool


