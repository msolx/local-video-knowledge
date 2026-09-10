import hashlib
import json
import os
import sys

REPO = r"G:\local_pc_project\personal-knowledge-pipeline"
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "src"))
os.chdir(REPO)

from src.provenance import load_evidence_manifest, verify_evidence_manifest
from src.chunking.service import load_evidence_chunks, verify_evidence_chunks
from src.knowledge.models import CanonicalKnowledgeUnitsDocument

CANONICAL = ["douyin_7681603850364521734", "douyin_7682038498466993905"]

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

for cid in CANONICAL:
    kdir = os.path.join(REPO, "data", "processed", cid, "knowledge")
    d = os.path.join(REPO, "data", "processed", cid)
    print(f"=== {cid} ===")
    # M3
    m = load_evidence_manifest(d)
    c = load_evidence_chunks(d)
    print(f"M3 manifest_valid={verify_evidence_manifest(m) if m else False} items={len(m['evidence_items']) if m else 0}")
    print(f"M3 chunks_valid={verify_evidence_chunks(c) if c else False} chunks={len(c['chunks']) if c else 0}")
    # final units
    up = os.path.join(kdir, "knowledge_units.json")
    doc = CanonicalKnowledgeUnitsDocument.from_dict(json.load(open(up, encoding="utf-8")))
    units = doc.units
    print(f"FINAL units={len(units)} canonical_id={doc.canonical_id} schema={doc.schema_version}")
    print(f"FINAL knowledge_units_sha256={sha256_file(up)}")
    fp = os.path.join(kdir, "knowledge_finalization.json")
    print(f"FINAL finalization_sha256={sha256_file(fp)}")
    fin = json.load(open(fp, encoding="utf-8"))
    print(f"FINAL source_enriched_artifact_fingerprint={fin.get('source_enriched_artifact_fingerprint')}")
    print(f"FINAL finalization_fingerprint={fin.get('finalization_fingerprint')}")
    print(f"FINAL knowledge.md exists={os.path.isfile(os.path.join(kdir, 'knowledge.md'))}")
    # current intermediate state (reconstructed present?)
    for f in ["raw_extractions", "knowledge_candidates.json", "merged_knowledge_candidates.json", "enriched_knowledge_candidates.json"]:
        p = os.path.join(kdir, f)
        if os.path.isfile(p):
            try:
                sz = os.path.getsize(p)
                data = json.load(open(p, encoding="utf-8"))
                n = data.get("units") if isinstance(data.get("units"), list) else (len(data.get("candidates", [])) if isinstance(data, dict) else "?")
                print(f"  intermediate PRESENT {f} size={sz} count={n}")
            except Exception as e:
                print(f"  intermediate PRESENT {f} (unreadable: {e})")
        elif os.path.isdir(p):
            files = os.listdir(p)
            print(f"  intermediate DIR PRESENT {f} files={files}")

# M5 production store audit
from src.knowledge.store import list_ingested_assets, compute_store_revision
DB = r"data\knowledge\knowledge_store.sqlite3"
import sqlite3
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT canonical_id, source_artifact_fingerprint, unit_count FROM ingested_assets ORDER BY canonical_id").fetchall()
print("=== M5 PRODUCTION STORE ===")
print(f"store exists={os.path.isfile(DB)} user_version={conn.execute('PRAGMA user_version').fetchone()[0]}")
for r in rows:
    print(f"  asset canonical_id={r['canonical_id']} fp={r['source_artifact_fingerprint']} unit_count={r['unit_count']}")
total_ku = conn.execute("SELECT COUNT(*) FROM knowledge_units").fetchone()[0]
print(f"total knowledge_units={total_ku}")
print(f"store_revision={compute_store_revision(DB)}")
conn.close()