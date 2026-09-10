import json
import os
import sqlite3
import sys

REPO = r"G:\local_pc_project\personal-knowledge-pipeline"
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "src"))
os.chdir(REPO)

DB = os.path.join(REPO, "data", "knowledge", "knowledge_store.sqlite3")

CANONICAL = ["douyin_7681603850364521734", "douyin_7682038498466993905"]

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

total = 0
mismatch = 0
for cid in CANONICAL:
    units_path = os.path.join(REPO, "data", "processed", cid, "knowledge", "knowledge_units.json")
    final_doc = json.load(open(units_path, encoding="utf-8"))
    final_units = {u["knowledge_unit_id"]: u for u in final_doc["units"]}

    rows = conn.execute(
        "SELECT knowledge_unit_id, canonical_payload_json FROM knowledge_units WHERE canonical_id=? ORDER BY unit_rowid",
        (cid,),
    ).fetchall()

    m5_payloads = {}
    for r in rows:
        p = json.loads(r["canonical_payload_json"])
        m5_payloads[p["knowledge_unit_id"]] = p

    print(f"=== {cid} ===")
    print(f"  M5 units={len(m5_payloads)} final units={len(final_units)}")
    common = set(m5_payloads) & set(final_units)
    only_m5 = set(m5_payloads) - set(final_units)
    only_final = set(final_units) - set(m5_payloads)
    print(f"  common_ids={len(common)} only_in_m5={sorted(only_m5)} only_in_final={sorted(only_final)}")

    semantic_diff = 0
    for uid in sorted(common):
        a = m5_payloads[uid]
        b = final_units[uid]
        # compare all canonical fields except nothing — semantic-equivalent = deep equal
        if a != b:
            semantic_diff += 1
            if semantic_diff <= 3:
                keys = set(a) | set(b)
                diffs = {k for k in keys if a.get(k) != b.get(k)}
                print(f"  DIFF {uid} fields={sorted(diffs)}")
    total += len(common)
    mismatch += semantic_diff + len(only_m5) + len(only_final)
    print(f"  semantic_diff={semantic_diff}")

print(f"\nTOTAL common={total} MISMATCHES={mismatch}")
conn.close()
print("PARITY=" + ("PASS" if (total == 68 and mismatch == 0) else "FAIL"))