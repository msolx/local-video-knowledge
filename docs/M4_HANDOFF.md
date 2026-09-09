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

## 2. Architectural Design Summary (M4-00)

Milestone M4 establishes the bridge between raw grounded sensory evidence (ASR speech segments and OCR visual text/frames) and structured, typed, attributed knowledge units.

The complete design specification is frozen in [`docs/M4_KNOWLEDGE_MODEL_DESIGN.md`](file:///G:/local_pc_project/personal-knowledge-pipeline/docs/M4_KNOWLEDGE_MODEL_DESIGN.md).

### 2.1 Core Architectural Principles
1. **Evidence-First Grounding**: Every `KnowledgeUnit` must reference 1:N atomic `EvidenceItem` IDs (`evidence_manifest.json`). Unanchored or hallucinated units are rejected.
2. **Chunk is a Processing Unit, Not a Provenance Unit**: Processing windows (`EvidenceChunk`) may overlap and vary in size, but canonical provenance strictly binds to underlying `EvidenceItem` records.
3. **Strict Epistemic Separation**: Extraction confidence measures parsing and formatting precision, **never** factual truth probability. All newly extracted units default to `verification_status: "not_checked"`.
4. **Author Claim vs Opinion Semantics**:
   - `author_claim`: Statements with objective truth conditions that can in principle be falsified or empirically verified.
   - `author_opinion`: Subjective viewpoints, qualitative recommendations, personal preferences, or speculative forecasts.
5. **Deterministic Knowledge Unit Identity**: `knowledge_unit_id` is generated via SHA-256 hash over canonical ID, unit type, sorted evidence IDs, and normalized statement (`ku_<digest[:16]>`).
6. **Multi-tier Overlap Deduplication**: Cross-chunk boundary duplication is resolved via exact evidence-set matching, superset absorption, and normalized statement unification.

---

## 3. Legacy Code Reconciliation Matrix

| Component | Status | Action Plan |
| :--- | :---: | :--- |
| `src/knowledge/chunker.py` | **`DEPRECATE`** | Superseded by `src/chunking/` (`evidence_chunks.json`). Will be phased out. |
| `src/knowledge/lifecycle.py` | **`KEEP`** | Preserves GPU VRAM orchestration for LM Studio (`lms ps`, `lms load`, `lms unload`). |
| `src/knowledge/service.py` | **`ADAPT`** | Refactor to consume M3 `evidence_manifest.json` and `evidence_chunks.json`. |
| `src/backends/llm.py` | **`ADAPT`** | Upgrade prompt and structured schema to `knowledge-units-v1`. |
| `src/render.py` | **`ADAPT`** | Update markdown rendering for `knowledge.md` to format typed KnowledgeUnits. |

---

## 4. Next Agent Protocol (NEXT_AGENT_START_HERE)

```text
================================================================================
                    NEXT_AGENT_START_HERE (CROSS-AGENT PROTOCOL)
================================================================================
Target Audience: Any LLM / Agent (Gemini 3.8 Flash, OpenCode + GLM 5.3, Codex)
Current Status : M4-00 Contract Design is DONE.
Next Task      : M4-01: Canonical Model & Domain Layer (READY TO COMMENCE)

1. CURRENT BRANCH:
   feat/m4-unified-knowledge-model

2. BASE COMMIT:
   1f1c3b9a604d2fbdb9bb63606d6392aa893c080e (main / m3-media-integration-complete)

3. KEY DESIGN SPECIFICATION TO READ:
   - docs/M4_KNOWLEDGE_MODEL_DESIGN.md (authoritative schema and taxonomy)
   - docs/M4_DECISIONS.md (Architectural Decisions 1-9)
   - docs/M4_TASKS.md (Task progression matrix)

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
================================================================================
```
