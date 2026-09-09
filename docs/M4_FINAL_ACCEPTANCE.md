# Milestone M4: Unified Knowledge Model · Final End-to-End Acceptance Report

> **Milestone Status**: `COMPLETE` (M4-06 `DONE`)  
> **Repository Root**: `G:\local_pc_project\personal-knowledge-pipeline`  
> **Git Branch**: `feat/m4-unified-knowledge-model`  
> **Pre-Acceptance HEAD**: `a7e132e10c876b932c24990c10a8f12110badb51` (M4-05)  
> **Acceptance Date**: 2026-09-09  
> **Target Scope**: End-to-End Verification of `M3 Evidence -> M4-02 Candidates -> M4-03 Merge -> M4-04 Enrichment -> M4-05 Finalization`  
> **Mode**: 100% offline, deterministic, read-only. No LLM runtime started or probed.

---

## 1. M4 Scope

Milestone M4 (`Unified Knowledge Model`) transforms the M3 grounded evidence
layer (`evidence_manifest.json`, `evidence_chunks.json`) into canonical,
deterministic, auditable **Knowledge Units**:

```text
M3 Evidence (speech / visual_text / visual_description)
      │
      ▼
M4-02 knowledge_candidates.json          (chunk-local grounded candidates)
      │
      ▼
M4-03 merged_knowledge_candidates.json   (exact KU-ID dedup / conflict isolation)
      │
      ▼
M4-04 enriched_knowledge_candidates.json (surface-grounded entities + topics)
      │
      ▼
M4-05 knowledge_units.json + knowledge.md + knowledge_finalization.json
      (canonical knowledge-units-v1 document + internal audit render)
```

M4 deliberately does **not** perform fact verification, global entity graphs,
Wikidata/Wikipedia linking, embeddings, RAG, or Obsidian publishing. Those
remain out of scope.

---

## 2. Sealed Commit Chain (M4-00 ~ M4-05)

| Step | Commit | Title |
| :--- | :--- | :--- |
| M4-00/01 | `29dc8f8` | canonical knowledge domain model (knowledge-units-v1) |
| M4-02 | `0294db4`, `6f6963b`, `6a0dd68` | grounded knowledge extraction pipeline + lineage hardening |
| M4-03 | `61ba5f6` | deterministic cross-chunk knowledge merging |
| M4-04 | `9423591` | grounded entity and topic enrichment |
| M4-05 | `a7e132e` | finalize canonical knowledge audit artifacts |

---

## 3. Real C10 Assets Under Acceptance

| Asset | Content Type | Manifest Items | Chunks |
| :--- | :--- | :---: | :---: |
| `douyin_7681603850364521734` | video (speech-only) | 184 | 4 |
| `douyin_7682038498466993905` | image album (3 OCR + 1 unresolved VLM) | 4 | 1 |

---

## 4. Artifact Chain Verification (from real disk, read-only)

### 4.1 C10 Video (`douyin_7681603850364521734`)

| Stage | Artifact | Unit / Candidate Count |
| :--- | :--- | :---: |
| M3 | `evidence_manifest.json` | 184 evidence items |
| M3 | `evidence_chunks.json` | 4 chunks (48/50/50/42 refs, 190 total, 184 unique) |
| M4-02 | `knowledge_candidates.json` | **62** candidates |
| M4-03 | `merged_knowledge_candidates.json` | **62** merged units |
| M4-04 | `enriched_knowledge_candidates.json` | **62** enriched units |
| M4-05 | `knowledge_units.json` | **62** final units |

### 4.2 C10 Album (`douyin_7682038498466993905`)

| Stage | Artifact | Unit / Candidate Count |
| :--- | :--- | :---: |
| M3 | `evidence_manifest.json` | 4 evidence items |
| M3 | `evidence_chunks.json` | 1 chunk (4 refs) |
| M4-02 | `knowledge_candidates.json` | **6** candidates |
| M4-03 | `merged_knowledge_candidates.json` | **6** merged units |
| M4-04 | `enriched_knowledge_candidates.json` | **6** enriched units |
| M4-05 | `knowledge_units.json` | **6** final units |

Stage-count identity holds for both assets: `merged == candidates`,
`enriched == merged`, `final == enriched`. **No stale artifact observed.**

---

## 5. Fingerprint Chain Audit (programmatic)

Every downstream stage references the content fingerprint of its true
upstream artifact. All recomputed on disk:

| Link | Video | Album | Result |
| :--- | :---: | :---: | :---: |
| chunks `source_manifest_fingerprint` == manifest fp | `943bac2c…` | `73471334…` | **MATCH** |
| candidates `provenance.evidence_manifest_fingerprint` | `943bac2c…` | `73471334…` | **MATCH** |
| candidates `provenance.evidence_chunks_fingerprint` | `5b4004cf…` | `bfb831b1…` | **MATCH** |
| merged `source_candidates_artifact_fingerprint` | `05eacb58…` | `d60c573c…` | **MATCH** |
| enriched `source_merged_artifact_fingerprint` | `8937799a…` | `f1433080…` | **MATCH** |
| finalization `source_enriched_artifact_fingerprint` | `0b329ed0…` | `6687bfd2…` | **MATCH** |
| finalization `finalization_fingerprint` recomputed | `59252f70…` | `93de51f6…` | **MATCH** |

No fingerprint chain break. M4-06 did **not** regenerate any prior stage.

---

## 6. Final KU ID Re-computation

All 68 final units (62 video + 6 album) re-hashed with the frozen formula:

```text
ku_<sha256(schema_version | canonical_id | unit_type | ordered_eids | normalized_statement)>[:16]
```

- Checked: **68**
- Mismatch: **0**
- Result: **100% match**

---

## 7. Full Evidence Grounding Audit (no sampling)

| Check | Video | Album |
| :--- | :---: | :---: |
| KU count audited | 62 | 6 |
| EvidenceRef count audited | 144 | 6 |
| `evidence_id` exists in real manifest | 0 violations | 0 |
| `evidence_id` legal within lineage chunk membership | 0 violations | 0 |
| `source_excerpt` == authoritative semantic payload | 0 violations | 0 |
| `temporal_range` identical to manifest | 0 violations | 0 |
| `sequence_range` identical to manifest | 0 violations | 0 |
| empty `source_excerpt` | 0 | 0 |
| unresolved visual evidence used for grounding | 0 | 0 |
| evidence ordering regressions (canonical order) | 0 | 0 |

Notes:
- Video excerpts match `payload.text` byte-for-byte for all 144 references.
- Album uses only `ve_img_001`/`ve_img_002` (resolved OCR). The unresolved
  `ve_vlm_img_003` (`status: unresolved_visual_reference`) is **never** used
  for semantic grounding, and the empty-OCR `ve_img_003` is never used either.

---

## 8. Attribution Audit

| Asset | Status | Speaker fields |
| :--- | :--- | :--- |
| Video (62 speech units) | `unverified_speaker` × 62 | `speaker_name`/`speaker_id` = null |
| Album (6 visual units) | `visual_media` × 6 | `speaker_name`/`speaker_id` = null |

- No `source_actor` was copied into `speaker`.
- No `verification_question` units exist in either asset (so no
  `system_derived` status is expected).
- Violations: **0**.

---

## 9. Observation Audit

- Video observations: **0** (speech-only asset correctly produces none).
- Album observations: **0**.
- No unit type `observation` exists, therefore no invalid speech/empty-OCR
  grounding can hide behind it. Gate satisfied.

---

## 10. Verification Status Audit

| Status | Video | Album |
| :--- | :---: | :---: |
| `not_checked` | 62 | 6 |
| `verified` | 0 | 0 |
| `contested` | 0 | 0 |
| `unsupported` | 0 | 0 |

No stage upgraded verification status based on confidence, evidence count, or
entity enrichment. `extraction_confidence` values remain in `[0.8, 1.0]`.

---

## 11. Entity Grounding Audit

All final `EntityMention`s checked with M4-04 normalization
(Unicode NFKC + casefold + whitespace collapse), requiring direct textual
support in `statement` or a `source_excerpt`. No fuzzy/alias/external
completion is accepted.

| Asset | Mentions | Grounded | Violations |
| :--- | :---: | :---: | :---: |
| Video | 132 | 132 | 0 |
| Album | 6 | 6 | 0 |

- Video: 62/62 units carry ≥1 entity; 112 distinct `(name, category)` pairs.
- Album: 6/6 units carry exactly the OCR surface text
  (`logitech`, `INAMAX`, `lognach`, `AGON`, `SMILEY`, `081`) — no company
  identity, sponsorship, or product relationship inferred.

---

## 12. Topic Policy Audit

| Asset | Units w/ ≥1 topic | Total topics | Violations |
| :--- | :---: | :---: | :---: |
| Video | 62 | 97 | 0 |
| Album | 6 | 6 | 0 |

- All topics within `0–5` per KU, `2–32` normalized chars, whitespace
  normalized, no intra-KU duplicates.
- Topics are structural classification labels only; no re-judgment applied.

---

## 13. Lineage Audit

| Asset | Units checked | Orphan lineage | Invalid chunk ref | Invalid candidate ref |
| :--- | :---: | :---: | :---: | :---: |
| Video | 62 | 0 | 0 | 0 |
| Album | 6 | 0 | 0 | 0 |

- Every `extraction_run_id` matches the M4-02 artifact.
- Every `input_chunk_ids` entry is a real chunk id.
- Every `source_candidate_ids` entry traces to a real processing candidate in
  `knowledge_candidates.json`.
- `merge_strategy = null` on all units (exact KU-ID merge applied, no conflict).

---

## 14. Cross-Stage Identity Audit

- **M4-03 -> M4-04**: all canonical fields
  (`knowledge_unit_id`, `canonical_id`, `unit_type`, `statement`,
  `evidence_refs`, `attribution`, `extraction_confidence`,
  `verification_status`, `extraction_lineage`) identical; only
  `entities`/`topics` added. **0 violations**.
- **M4-04 -> M4-05**: all 11 canonical fields (including `entities`/`topics`)
  byte-identical. **0 violations**.

---

## 15. Knowledge.md / JSON Parity

| Asset | JSON units | Markdown-rendered KUs | Missing | Extra |
| :--- | :---: | :---: | :---: | :---: |
| Video | 62 | 62 | 0 | 0 |
| Album | 6 | 6 | 0 | 0 |

Every final KU appears exactly once in `knowledge.md`; no additional
KnowledgeUnit is generated by the render.

---

## 16. Qualitative Sample Audit (Video)

Deterministic sample of 15 units:
- first 3, middle 3, last 3
- lowest `extraction_confidence` 3 (all `0.8`)
- most-evidence 3 (`6ev`, `6ev`, `4ev`)

Result: **A = 13, B = 2, C = 0, D = 0, E = 0**.

| Class | Count | Definition |
| :--- | :---: | :--- |
| A clearly grounded & correctly typed | 13 | measurement/experience statements backed by excerpts |
| B grounded but type questionable | 2 | advisory statements typed as `claim` |
| C overgeneralized beyond evidence | 0 | — |
| D unsupported / hallucinated | 0 | — |
| E duplicate / fragmentary / low-value | 0 | — |

### Questionable (B) units

| KU ID | Reason |
| :--- | :--- |
| `ku_450bbaec3088a1d2` | "选择适配Strax的Rocom量化模型是选模型的大原则。" — advice/principle phrased as a claim; grounded in excerpt but arguably `opinion`. |
| `ku_73956456112cb153` | "以后下载模型的时候，不要只看Q4这两字…" — imperative advice typed as `claim`. |

---

## 17. Claim / Opinion / Procedure Classification Check

Marker scan (建议/最好/应该/第一步/不要只看/要优化/原则…) across all 62 video
units flagged **4** units whose phrasing is advisory or procedural but typed as
`claim`:

| KU ID | Marker | Statement fragment |
| :--- | :--- | :--- |
| `ku_cd2c84d746aa6a28` | 要优化 / 必须 | 要优化这台设备上的模型表现，必须围绕… |
| `ku_bfd600d997ff9258` | 第一步 / 先来 | 第一步我们先来确定软件的运行环境。 |
| `ku_450bbaec3088a1d2` | 原则 | …是选模型的大原则。 |
| `ku_73956456112cb153` | 不要只看 | 以后下载模型的时候，不要只看Q4这两字… |

This is **occasional minor misclassification** (~4/62 ≈ 6.5%), not a
systematic error. The schema remains valid; per acceptance policy M4 is
accepted **with this known limitation**. The extractor is **not** modified in
M4-06.

---

## 18. Album Qualitative Audit

All 6 final album units state `"The text 'X' appears in the visual content."`
for the exact OCR surface strings. Grounded: **YES × 6**.

- No company identity, sponsorship, product relationship, or team affiliation
  is derived from `logitech`, `INAMAX`, `AGON`, `SMILEY`, `081`.
- No unresolved VLM-derived visual entity is invented.
- Attribution is `visual_media` throughout; statements stay at the
  observation-of-text level.

---

## 19. Duplication / Low-Value Observations

- M4-03 deduplicated on exact KU ID only; no semantic merge was performed.
- Direct text audit (normalized-statement similarity > 0.8): **0 near-duplicate
  pairs** in the final video set; all 62 statements distinct.
- Low-value meta statements present in video: `ku_f7bee5094a3efa30`
  (部署清单已发布), `ku_b8b32be1593951cf` (下一期预告), `ku_d1273cff79cf8880`
  (内容目的说明) — these are grounded creator announcements with low epistemic
  weight. Recorded as a **known limitation**; no semantic cleanup performed.

---

## 20. No Output Mutation

M4-06 verified and reported on the real artifacts. No
`knowledge_candidates`, `merged`, `enriched`, `knowledge_units`, or
`knowledge.md` file was modified. The audit runner
(`scripts/run_m4_06_acceptance.py`) is read-only toward all knowledge
artifacts and writes only a machine-readable summary under the gitignored
`data/acceptance/` directory, explicitly tagged
`"knowledge_layer": false`.

---

## 21. Regression Baselines

### 21.1 Targeted M4 Suites
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_models.py tests/test_knowledge_extraction.py tests/test_knowledge_dedup.py tests/test_knowledge_enrichment.py tests/test_knowledge_render.py tests/test_m4_acceptance.py -v
```
- **243 passed in 0.66s** (models + extraction + dedup + enrichment + render
  + 33 new acceptance tests).

### 21.2 Full Regression
```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```
- **986 passed, 10 skipped in 56.88s** (M3 baseline 953 + 33 new acceptance
  tests; exactly reconciled, zero regressions).

---

## 22. M2/M3 Integrity

- `git diff --stat <m4-head> -- src/collector/ src/downloader/` : **empty** —
  M2/M3 code untouched.
- M3 evidence artifacts (`evidence_manifest.json`, `evidence_chunks.json`)
  were treated strictly read-only.

---

## 23. Known Limitations

1. **Minor claim/opinion/procedure misclassification** (~4/62 video units,
   6.5%) — advisory/procedural phrasing typed as `claim`. Acceptable per
   acceptance policy; deferred to a future classifier refinement, never to be
   silently re-run in M4.
2. **Meta / announcement units** (3 video units) — grounded creator framing
   with low epistemic weight; retained as extracted.
3. **No semantic near-duplicate merge** — M4-03 uses exact KU-ID dedup by
   design; semantically similar but textually distinct statements can coexist.
4. **ASR artifacts** — statement-level "TB"/token unit noise from source ASR
   (e.g. `ku_4a5bdd60d02d7ba1`) is preserved verbatim per the excerpt contract
   and is an upstream M3 concern, not an M4 grounding failure.

---

## 24. Final Acceptance Decision

**M4-06: ACCEPT (M4 milestone COMPLETE with known limitations).**

All structural gates passed:
- fingerprint chain intact (no stale artifacts)
- KU ID recomputation 100%
- evidence grounding, attribution, observation, verification, entity, topic,
  lineage, cross-stage identity, and Markdown/JSON parity audits: **0 violations**
- full regression green (986 passed, 10 skipped)
- M2/M3 frozen code and M3 evidence artifacts untouched

No hallucinated KUs, no systematic classification errors, no broken grounding
or lineage, no fingerprint mismatch.

---

## 25. Signoff Statement

Milestone M4 (**Unified Knowledge Model**) is hereby declared **COMPLETE**
(`M4-06 DONE`). The end-to-end chain from M3 grounded evidence to canonical
`knowledge_units.json` + `knowledge.md` is deterministic, offline, auditable,
and accepted for both real C10 assets.

Awaiting milestone integration / next milestone definition.