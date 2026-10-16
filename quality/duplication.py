#!/usr/bin/env python3
"""Share of duplicated lines in the changed files, from a jscpd JSON report (the gate's "new code" view).

usage: duplication.py <jscpd-report.json> <changed-files.txt> <run-dir>
"""
import json
import os
import sys

report, changed_list, run_dir = sys.argv[1:4]
with open(changed_list, encoding="utf-8") as handle:
    changed = {line.strip() for line in handle if line.strip()}
with open(report, encoding="utf-8") as handle:
    duplicates = json.load(handle).get("duplicates", [])

duplicated = {}
for dup in duplicates:
    for side in ("firstFile", "secondFile"):
        f = dup[side]
        name = os.path.relpath(f["name"]) if os.path.isabs(f["name"]) else f["name"].lstrip("./")
        if name in changed:
            duplicated.setdefault(name, set()).update(range(f["start"], f["end"] + 1))

total = 0
for name in changed:
    try:
        with open(name, encoding="utf-8", errors="ignore") as handle:
            total += sum(1 for _ in handle)
    except OSError:
        pass
dup_lines = sum(len(lines) for lines in duplicated.values())
pct = 100.0 * dup_lines / total if total else 0.0
os.makedirs(run_dir, exist_ok=True)
with open(os.path.join(run_dir, "duplication.json"), "w", encoding="utf-8") as handle:
    json.dump({"pct": round(pct, 2), "duplicated_lines": dup_lines, "lines": total,
               "files": sorted(duplicated)}, handle)
print(f"duplication in changed files: {pct:.1f}% ({dup_lines} of {total} lines)")
