#!/usr/bin/env python3
"""Turn an OpenGrep JSON report (already limited to new findings by --baseline-commit) into the gate's input.

usage: opengrep_new.py <opengrep.json> <run-dir> <had-baseline: true|false>

Every finding is printed to the job log; blocking ones (severity ERROR with HIGH confidence) are annotated.
"""
import json
import os
import sys

report, run_dir, had_baseline = sys.argv[1], sys.argv[2], sys.argv[3] == "true"
with open(report, encoding="utf-8") as handle:
    results = json.load(handle)["results"]
blocking = []
for r in results:
    extra = r["extra"]
    severity = extra.get("severity")
    confidence = (extra.get("metadata") or {}).get("confidence", "")
    print(f"{severity:8} {confidence:7} {r['path']}:{r['start']['line']}  {r['check_id']}")
    if severity == "ERROR" and str(confidence).upper() == "HIGH":
        item = {"rule": r["check_id"].split(".")[-1], "path": r["path"], "line": r["start"]["line"]}
        blocking.append(item)
        print(f"::error file={item['path']},line={item['line']}::{r['check_id']}")
os.makedirs(run_dir, exist_ok=True)
with open(os.path.join(run_dir, "opengrep.json"), "w", encoding="utf-8") as handle:
    json.dump({"new": len(results), "blocking": blocking, "baseline": had_baseline}, handle)
