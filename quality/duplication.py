#!/usr/bin/env python3
"""Share of duplicated lines in the new code, from a jscpd JSON report (the gate's "new code" view).

usage: duplication.py <jscpd-report.json> <changed-files.txt> <run-dir> [changed-lines.diff]

With the `git diff -U0` of the push, only the lines it added count: touching a file that already holds
duplicated code must not fail the gate for code the change did not write. Without it (no previous commit,
the whole repository is new code) every line of the changed files counts.
"""
import json
import os
import re
import sys

report, changed_list, run_dir = sys.argv[1:4]
diff_path = sys.argv[4] if len(sys.argv) > 4 else None
with open(changed_list, encoding="utf-8") as handle:
    changed = {line.strip() for line in handle if line.strip()}
with open(report, encoding="utf-8") as handle:
    duplicates = json.load(handle).get("duplicates", [])

added = None
if diff_path:
    added, current = {}, None
    with open(diff_path, encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if line.startswith("+++ "):
                current = line[6:].rstrip("\n") if line.startswith("+++ b/") else None
            elif current and line.startswith("@@"):
                m = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", line)
                if m:
                    start, count = int(m.group(1)), int(m.group(2) or 1)
                    added.setdefault(current, set()).update(range(start, start + count))

duplicated = {}
for dup in duplicates:
    for side in ("firstFile", "secondFile"):
        f = dup[side]
        name = os.path.relpath(f["name"]) if os.path.isabs(f["name"]) else f["name"].lstrip("./")
        if name in changed:
            duplicated.setdefault(name, set()).update(range(f["start"], f["end"] + 1))

if added is not None:
    total = sum(len(lines) for name, lines in added.items() if name in changed)
    dup_lines = sum(len(lines & added.get(name, set())) for name, lines in duplicated.items())
else:
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
print(f"duplication in {'new lines' if added is not None else 'changed files'}: {pct:.1f}% ({dup_lines} of {total} lines)")
