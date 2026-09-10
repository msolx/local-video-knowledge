import hashlib
import os
import sys

REPO = r"G:\local_pc_project\personal-knowledge-pipeline"
BACKUP = r"G:\pkp_backup\m4_incident_post_reconstruction_20260910"
MANIFEST = os.path.join(BACKUP, "manifest.txt")

entries = {}
with open(MANIFEST, encoding="utf-8") as f:
    for line in f:
        parts = line.strip().split(None, 2)
        if len(parts) == 3:
            entries[parts[2]] = (parts[0], int(parts[1]))

print(f"manifest entries={len(entries)}")
mismatch = 0
checked = 0
for rel, (expected_sha, expected_size) in sorted(entries.items()):
    # manifest rel paths are douyin_<cid>/<rest> (mirror of knowledge/ dir)
    cid, sep, rest = rel.partition("/")
    live_path = os.path.join(REPO, "data", "processed", cid, "knowledge", rest)
    backup_path = os.path.join(BACKUP, rel)
    for tag, path in (("live", live_path), ("backup", backup_path)):
        if not os.path.isfile(path):
            print(f"  MISSING {tag} {rel}")
            mismatch += 1
            continue
        data = open(path, "rb").read()
        sha = hashlib.sha256(data).hexdigest().upper()
        size = len(data)
        ok = sha == expected_sha and size == expected_size
        checked += 1
        if not ok:
            print(f"  MISMATCH {tag} {rel}: got sha={sha[:16]}... size={size} expected {expected_sha[:16]}.../{expected_size}")
            mismatch += 1

print(f"checked={checked} mismatches={mismatch}")
print("BACKUP_VERIFY=" + ("PASS" if mismatch == 0 else "FAIL"))