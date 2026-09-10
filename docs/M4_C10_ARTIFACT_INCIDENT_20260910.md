# M4 C10 Artifact Incident — 2026-09-10

**Status:** RECOVERED_WITH_INTERMEDIATE_PROVENANCE_LOSS
**Incident ID:** `m4-c10-provenance-incident-20260910`
**Machine-readable record:** `tests/fixtures/m4_c10_incident_20260910.json`
**Affected assets:** `douyin_7681603850364521734`, `douyin_7682038498466993905`

This document is an objective, non-minimized record of the incident. It does not
dilute the root cause, the damage, or the permanent consequences.

---

## 1. Incident root cause

On 2026-09-10, an M6-05 recovery test (`_build_c10_chain_to_searchable` in
`tests/test_operations_recovery.py`) invoked the real M4
`KnowledgeExtractAdapter` against the **live** `data/processed/` tree instead of a
disposable copy. On cache miss the M4 extractor chain re-ran with a
`MockLLMBackend` (which produces **zero** candidates) and overwrote the sealed
M4-02/03/04 intermediate artifacts via `atomic_write_json`. The M4 cache
fingerprint at that moment did not match the stored fingerprint (the sealed
artifacts were produced by the historical real LM Studio run), so the
destructive re-run was not prevented by the normal cache guard.

Root cause class: **test executed a destructive stage adapter against live
production data** (no disposable-root isolation, no destructive-target guard).

## 2. Overwritten files (lost)

For both C10 assets, under `data/processed/<canonical_id>/knowledge/`:

- `raw_extractions/chk_*.json` (became `status=cached`, `raw_response={'candidates': []}`)
- `knowledge_candidates.json` (became 0 candidates)
- `merged_knowledge_candidates.json` (became 0 units)
- `enriched_knowledge_candidates.json` (became 0 units)

These were subsequently replaced (see §7) by **reconstructed** intermediates for
test/offline-chain continuity. They are **not** the historical original artifacts.

## 3. Preserved files (survived intact, sealed 2026-09-09)

- `knowledge_units.json` — canonical final knowledge (video 62 units, album 6 units)
- `knowledge_finalization.json` — finalization metadata incl. historical anchors
- `knowledge.md` — final render
- `evidence_manifest.json`, `evidence_chunks.json` — M3 evidence (video 184 items / 4 chunks, album 4 items / 1 chunk)

## 4. Forensic search

An exhaustive forensic search was performed for the byte-exact originals:

- `data/` is gitignored — no Git history backup exists (`git ls-files data/`, `git log --all -- data/` empty; reflog shows only milestone commits).
- Swept: `G:\local_pc_project\` (whole), `G:\pkp_backup\`, `G:\opencode_project\`
  (incl. all pytest temp dirs), `C:\Users\Sean\AppData\Local\Temp\pytest-of-Sean\`.
- 40 `enriched_knowledge_candidates.json` files fingerprinted — **none** match the frozen anchors.
- Post-incident disposable copies in pytest temp dirs contain only the wiped
  (0-candidate) state or synthetic fixtures.

## 5. Exact recovery failure

Byte-exact recovery is **mathematically impossible** because the artifact
fingerprints depend on the historical LLM execution itself:

- `compute_enriched_artifact_fingerprint` hashes the **entire** enriched artifact,
  including the real-LLM `enrichment_provenance` and `audit` block
  (llm_call_count, batch summaries).
- Candidate `knowledge_unit_id`/`candidate_id` hashes derive from the historical
  LLM raw candidate dicts.

No reconstruction can reproduce the frozen historical anchors
(`0b329ed0…` video, `6687bfd2…` album).

## 6. Real rerun result (Phase-1, forensic only)

An isolated real re-execution was performed with the exact historical runtime
(LM Studio, model `qwen3-8b` = `Qwen3-8B-Q4_K_M.gguf`, SHA-256
`A7676D25…`, backend `openai_compatible`, `m4-extraction-v1.0`,
temperature 0.1, max_tokens 4096) into an isolated recovery root
(`G:\pkp_backup\m4_incident_real_rerun_20260910\`):

| asset | historical | rerun |
|-------|-----------|-------|
| video | 62 units | **69 units** |
| album | 6 units | 6 units |

- video rerun enriched fingerprint: `9ca8d656…`
- album rerun enriched fingerprint: `7c8d8132…`

## 7. Why the rerun was rejected as canonical replacement

The real rerun is **not** equivalent to the historical run. It produced
**69** units for the video (vs. historical 62): 29 units removed, 36 added,
33 shared. This is expected LLM nondeterminism (Qwen3-8B reasoning model, GPU
sampling), and it means a fresh run generates a **different** knowledge
generation — it is not a faithful reproduction of the lost execution.

Adopting it would (a) fabricate a new canonical generation that never
historically existed, and (b) break the frozen M4 identity contract against the
surviving final. Therefore the rerun is **forensic evidence only** and does not
enter production.

## 8. Preserved canonical final policy

- The surviving `knowledge_units.json` / `knowledge_finalization.json` /
  `knowledge.md` for both assets remain the **historical canonical final state**.
- The historical frozen `source_enriched_artifact_fingerprint` anchors
  (`0b329ed0…` video, `6687bfd2…` album) are **retained** — they describe the
  original, now-lost execution generation. They are **not** overwritten and not
  re-anchored to any reconstruction or rerun.
- M4 code semantics remain **SEALED**.
- M4 historical fixture status:
  **RECOVERED_WITH_INTERMEDIATE_PROVENANCE_LOSS**.

## 9. M5 impact

**NONE.** The production M5 store
(`data/knowledge/knowledge_store.sqlite3`) retains the original 68 knowledge
units (62 video + 6 album). A per-KU audit confirmed **68/68** canonical
payloads are semantically identical to the surviving `knowledge_units.json`.
The M5 store was **not** rebuilt, replaced, ingested, vacuumed, or modified.
`store_revision` = `7b604b334eaaede2f98e341a1cbeafdf3979d4643bc2b54196769e19919355d6`.

## 10. Known permanent provenance loss

The historical M4 raw/candidate/merged/enriched **execution artifacts** for both
C10 assets are permanently lost. The chain is no longer byte-reproducible. All
M3 evidence and M4 canonical final artifacts survive and are schema-valid.

## 11. Treatment of reconstructed and rerun artifacts

- **Reconstructed intermediates** (`raw_extractions/*`,
  `knowledge_candidates.json`, `merged_knowledge_candidates.json`,
  `enriched_knowledge_candidates.json` present in the live tree) are
  **non-canonical**. They exist only for sealed offline test/chain continuity.
  They are fully backed up at
  `G:\pkp_backup\m4_incident_post_reconstruction_20260910\` (SHA-256 manifest
  verified against the live tree). They are **never** described as historical
  original artifacts.
- **Phase-1 isolated rerun** (`G:\pkp_backup\m4_incident_real_rerun_20260910\`)
  is **forensic-only**, never copied into `data/processed/`, never ingested into
  M5, never used as an anchor.

## 12. Future prevention

- Tests that execute M4 stage adapters must operate on a **disposable copy** of
  `data/processed/` (see the recovery test fix and the new destructive
  real-processed-root guard in `tests/test_m4_incident_recovery.py`).
- A generic guard rejects test-mode/destructive execution targets that resolve
  to the repository's real `data/processed/`.
- No hardcoded `canonical_id` special-casing was added to production code.

## 12b. M6 future-pipeline CACHE_HIT risk audit

**Question:** can the M6 stage adapters treat the reconstructed intermediates as
a legitimate historical `CACHE_HIT`?

**Finding: NO, at the extraction stage; the reconstructed chain is not accepted
as a full-chain cache hit. Residual risk is confined to test-mode mock runs and
is addressed by the destructive-root guard.**

Evidence (deterministic fingerprint recomputation against live files):

- **Extraction raw cache:** the reconstructed
  `raw_extractions/chk_000001.json` files carry `status=cached` with
  `cache_fingerprint` values (`213bfe36…` video, `2b68ae7e…` album) that do
  **not** reproduce under `compute_extraction_config_fingerprint` — neither with
  the recorded config nor with a real production config (recomputed
  `d4bb95e8…`/`0ac9f911…`). A fresh extraction therefore **cache-misses** on
  these files. The `cache_fingerprint` material includes `backend`, `model`,
  `base_url`, `prompt_version`, `temperature`, `max_tokens`, and the
  manifest/chunk fingerprints.
- **Merge + enrichment:** the reconstructed `merged`/`enriched` artifacts are
  internally self-consistent (recomputed `source_candidates_artifact_fingerprint`
  and `source_merged_artifact_fingerprint` match). This is only reachable after a
  successful extraction cache hit, which never occurs on these files.
- **Finalization:** `render.py` recomputes
  `compute_enriched_artifact_fingerprint(enriched)` and compares it to the cached
  `finalization_fingerprint`; a mismatch forces a fresh deterministic render.

**Consequence:** no production-code path can mistake the reconstructed bytes for
the historical originals. The only realistic residual risk is a **test-mode mock
run** targeting the live tree, which would re-extract 0 candidates and wipe the
chain again. That exact class is now rejected by the generic destructive
processed-root guard added in `tests/test_m4_incident_recovery.py`, at the test
boundary — no sealed M6-03 production contract change was required.