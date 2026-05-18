"""Typed helpers / constants for domain rule trees (schema mcg_domain_rule_tree.v1).

These are ergonomic hints only; payloads are validated as loose dicts by the builder/validator."""

from __future__ import annotations

from typing import Any, Literal, TypedDict


LogicOperator = Literal["AND", "OR", "CHECKLIST", "OPTIONS", "EXAMPLE_SET"]
ReviewStatus = Literal["auto_extracted", "needs_review"]
Confidence = Literal["high", "medium", "low"]

SCHEMA_DOMAIN_RULE_TREE = "mcg_domain_rule_tree.v1"
SCHEMA_SOURCE_TREE = "mcg_source_tree.v1"


class LogicBasis(TypedDict, total=False):
    raw_phrase: str
    confidence: Confidence


class SourceRefDict(TypedDict, total=False):
    source_ref_id: str
    mcg_code: str
    source_node_id: str
    source_section_id: str
    domain: str | None
    source_quote: str
    footnote_refs: list[str]
    reference_refs: list[str]
    text_hash: str


class DomainNodeDict(TypedDict, total=False):
    node_id: str
    level: int
    node_type: str
    name: str | None
    mcg_code: str
    mcg_title: str
    domain: str | None
    parent_node_id: str | None
    child_node_ids: list[str]
    sort_order: int
    description: str
    original_text: str
    normalized_text: str
    evaluation_mode: str
    logic_operator: LogicOperator | None
    logic_basis: LogicBasis | None
    logic_root_id: str | None
    source_node_ids: list[str]
    source_ref_ids: list[str]
    review_status: ReviewStatus
    warnings: list[str]


class LogicNodeDict(TypedDict, total=False):
    logic_node_id: str
    level: int
    logic_depth: int
    node_kind: Literal["atomic", "composite", "context"]
    linked_domain_node_id: str | None
    parent_logic_node_id: str | None
    child_logic_node_ids: list[str]
    sort_order: int
    operator: str | None
    display_label: str | None
    condition_key: str | None
    measurement: str | None
    value: Any
    unit: str | None
    evaluation_mode: str
    strict_boolean_evaluation: bool
    example_only: bool
    original_text: str
    normalized_text: str
    logic_basis: LogicBasis | None
    source_node_ids: list[str]
    source_ref_ids: list[str]
    review_status: ReviewStatus
    warnings: list[str]


class ConditionDictionaryEntryDict(TypedDict, total=False):
    condition_key: str
    condition_role: str
    mcg_code: str
    domain: str
    linked_domain_node_id: str | None
    linked_logic_node_id: str
    node_kind: str
    operator: str | None
    measurement: str | None
    value: Any
    unit: str | None
    original_text: str
    source_ref_ids: list[str]
    definition_scope: Literal["guideline_local", "shared_candidate"]
    llm_extractable: bool
    review_status: ReviewStatus


class DomainRuleTreeV1Dict(TypedDict, total=False):
    schema_version: str
    source_tree_schema_version: str
    mcg_code: str
    mcg_title: str
    source_tree_path: str
    source_document: dict[str, Any]
    domain_roots: dict[str, str]
    domain_nodes: list[dict[str, Any]]
    logic_nodes: list[dict[str, Any]]
    source_refs: list[dict[str, Any]]
    condition_dictionary: list[dict[str, Any]]
    audit: dict[str, Any]
