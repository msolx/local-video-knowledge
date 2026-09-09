# Milestone M4: Unified Knowledge Model · Master Handoff & Continuity Guide

> **Authoritative Handoff Document for Milestone M4**  
> **Repository Root**: `G:\local_pc_project\personal-knowledge-pipeline`  
> **Milestone Status**: `STARTED` (Design Phase M4-00 `DONE`)  
> **Current Focus**: Task M4-00 Contract Design (`DONE`)  
> **Next Focus**: Task M4-01 Canonical Model & Domain Layer (`TODO` - Ready for Implementation)  
> **Handoff Target**: Cross-Agent / Cross-Harness Compatible (Gemini 3.8 Flash, OpenCode + GLM 5.3, Codex)

---

## 1. Executive Status Snapshot

| Property | Value |
| :--- | :--- |
| **Milestone** | **Milestone M4: Unified Knowledge Model** |
| **Status** | **STARTED** (User authorized 2026-09-09) |
| **Current Task** | **M4-00: Unified Knowledge Model Contract Design** (`DONE`) |
| **Git Branch** | `feat/m4-unified-knowledge-model` |
| **Base Anchor** | `1f1c3b9a604d2fbdb9bb63606d6392aa893c080e` (Tag: `m3-media-integration-complete`, `main`) |
| **M2 Subsystem State** | **FROZEN / UNTOUCHED** (`src/collector/`, `src/downloader/`) |
| **M3 Subsystem State** | **FROZEN / STABLE CONTRACT** (`src/media_adapter/`, `src/chunking/`, `src/provenance.py`) |
| **Active Test Baseline** | **743 passed, 10 skipped** (Main `.venv`); **55 passed, 10 deselected** (Worker `.venv-f2`) |

---

## 2. Reconciled Contract Summary (M4-00 Final)

Milestone M4 establishes the bridge between raw grounded sensory evidence (ASR speech segments and OCR visual text/frames) and structured, typed, attributed knowledge units.

The authoritative design specification is frozen in [`docs/M4_KNOWLEDGE_MODEL_DESIGN.md`](file:///G:/local_pc_project/personal-knowledge-pipeline/docs/M4_KNOWLEDGE_MODEL_DESIGN.md).

### 2.1 Key Reconciled Contracts
1. **Decoupled Semantics & Attribution**:
   - Canonical types: `claim`, `opinion`, `observation`, `procedure_step`, `verification_question`.
   - Speaker attribution is completely orthogonal: `attribution_status = "source_author_explicit | named_speaker | quoted_third_party | unverified_speaker | visual_media | system_derived"`.
   - Regular ASR without diarization defaults to `unverified_speaker`, preventing false claims of verified author speech.
2. **Evidence Ref + Excerpt Encapsulation**:
   - `source_excerpt` is directly bound inside each `EvidenceRef`. No fragile parallel arrays.
3. **Canonical Evidence Ordering**:
   - `evidence_refs` maintains canonical manifest/chunk order. Lexical sorting is forbidden.
4. **Deterministic Knowledge Unit Identity**:
   - `ku_<sha256(schema_version + '|' + canonical_id + '|' + unit_type + '|' + canonical_ordered_eids + '|' + normalized_statement)[:16]>`.
5. **Extraction Confidence vs Truth**:
   - `extraction_confidence` represents parsing/extraction certainty, NOT truth probability.
   - `verification_status` defaults strictly to `"not_checked"`.
6. **Provenance Tracking**:
   - Records both `evidence_manifest_fingerprint` and `evidence_chunks_fingerprint`, along with `input_chunk_ids`.
7. **Relationships Removed from v1**:
   - Unused inter-unit relationships removed from v1 schema. Entities and topics retained as extension points.
8. **Markdown Render Scope**:
   - `knowledge.md` is strictly an internal audit document, NOT an Obsidian publishing layer.

---

## 3. Legacy Code Reconciliation Matrix

| Component | Status | Action Plan |
| :--- | :---: | :--- |
| `src/knowledge/chunker.py` | **`DEPRECATE`** | Superseded by `src/chunking/` (`evidence_chunks.json`). Will be phased out. |
| `src/knowledge/lifecycle.py` | **`KEEP`** | Preserves GPU VRAM orchestration for LM Studio (`lms ps`, `lms load`, `lms unload`). |
| `src/knowledge/service.py` | **`ADAPT`** | Refactor to consume M3 `evidence_manifest.json` and `evidence_chunks.json`. |
| `src/backends/llm.py` | **`ADAPT`** | Upgrade prompt and structured schema to `knowledge-units-v1`. |
| `src/render.py` | **`ADAPT`** | Update internal markdown rendering for `knowledge.md` to format typed KnowledgeUnits. |
| Legacy `author_claim` | **`MIGRATED`** | Maps to: `claim` + `attribution.attribution_status = "source_author_explicit"`. |
| Legacy `author_opinion` | **`MIGRATED`** | Maps to: `opinion` + `attribution.attribution_status = "source_author_explicit"`. |

---

## 4. Next Agent Protocol (NEXT_AGENT_START_HERE)

```text
================================================================================
                    NEXT_AGENT_START_HERE (CROSS-AGENT PROTOCOL)
================================================================================
Target Audience: Any LLM / Agent (Gemini 3.8 Flash, OpenCode + GLM 5.3, Codex)
Current Status : M4-00 Contract Design is DONE (Reconciled).
Next Task      : M4-01: Canonical Model & Domain Layer (READY TO COMMENCE)

1. CURRENT BRANCH:
   feat/m4-unified-knowledge-model

2. BASE COMMIT:
   1f1c3b9a604d2fbdb9bb63606d6392aa893c080e (main / m3-media-integration-complete)

3. KEY DESIGN SPECIFICATION TO READ:
   - docs/M4_KNOWLEDGE_MODEL_DESIGN.md (authoritative schema and taxonomy v1.1)
   - docs/M4_DECISIONS.md (Architectural Decisions 1-9)
   - docs/M4_TASKS.md (Corrected Task progression matrix)

4. M4-01 OBJECTIVE:
   Implement Canonical KnowledgeUnit dataclasses, enums, validation, and serialization
   in `src/knowledge/models.py`, accompanied by comprehensive unit tests in
   `tests/test_knowledge_models.py`.

5. INVARIANTS (DO NOT BREAK):
   - DO NOT modify M2 code (src/collector/, src/downloader/).
   - DO NOT modify M3 code (src/media_adapter/, src/chunking/, src/provenance.py).
   - DO NOT make network calls or live Douyin requests.
   - Archive files in archive/ must remain strictly read-only.
   - All knowledge units must default to verification_status = "not_checked".
   - Keep unit_type decoupled from attribution.
================================================================================
```
