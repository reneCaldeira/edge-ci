#!/usr/bin/env python3
"""Quality gate on new code, modelled on SonarQube's "Sonar way" gate.

The scan jobs of the same run leave their results in <run-dir> (a directory in the runner's HOME, shared by
every slot); this script reads them before the deployment and fails when a blocking condition is not met.

    gate.py check  <run-dir> <baseline.json>   evaluate, write the step summary, exit 1 on failure
    gate.py record <run-dir> <baseline.json>   after a successful deployment: this run's coverage becomes
                                               the new reference

Blocking: new OpenGrep findings of severity ERROR and HIGH confidence, new sonarjs bugs in changed files,
duplicated lines above DUPLICATION_MAX % of the changed files, total coverage below the last deployed run.
Reported only: code smells, linter findings, coverage against COVERAGE_TARGET.
"""
import json
import os
import sys

DUPLICATION_MAX = 10.0  # % of the lines in changed files that are duplicated
COVERAGE_TARGET = 60.0  # reported for now; the gate only refuses a drop below the last deployment
COVERAGE_TOLERANCE = 0.1  # percentage points: rounding noise is not a drop


def load(run, name):
    path = os.path.join(run, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def coverage_pct(run):
    summary = load(run, "coverage-summary.json")
    return None if summary is None else float(summary["total"]["lines"]["pct"])


def check(run, baseline_path):
    failures, notes = [], []

    opengrep = load(run, "opengrep.json")
    if opengrep is None:
        notes.append("OpenGrep: no result (scan skipped)")
    else:
        blocking = opengrep.get("blocking", [])
        notes.append(f"OpenGrep: {opengrep.get('new', 0)} new findings, {len(blocking)} blocking"
                     + ("" if opengrep.get("baseline") else " (no previous commit to compare: whole repository)"))
        for item in blocking:
            failures.append(f"OpenGrep `{item['rule']}` at `{item['path']}:{item['line']}`")

    sonar = load(run, "sonarjs.json")
    if sonar is not None:
        notes.append(f"SonarJS: {len(sonar.get('bugs', []))} bugs and {sonar.get('smells', 0)} code smells in changed files")
        for item in sonar.get("bugs", []):
            failures.append(f"SonarJS bug `{item['rule']}` at `{item['path']}:{item['line']}`")

    duplication = load(run, "duplication.json")
    if duplication is not None:
        pct = float(duplication.get("pct", 0.0))
        notes.append(f"Duplication in changed files: {pct:.1f}% (max {DUPLICATION_MAX:.0f}%)")
        if pct > DUPLICATION_MAX:
            failures.append(f"Duplication {pct:.1f}% in changed files is above {DUPLICATION_MAX:.0f}%")

    pct = coverage_pct(run)
    if pct is None:
        notes.append("Coverage: no result (tests skipped)")
    else:
        reference = None
        if os.path.exists(baseline_path):
            with open(baseline_path, encoding="utf-8") as handle:
                reference = json.load(handle).get("coverage")
        target = "reached" if pct >= COVERAGE_TARGET else "not reached yet"
        if reference is None:
            notes.append(f"Coverage: {pct:.1f}% (first measurement, becomes the reference; target {COVERAGE_TARGET:.0f}% {target})")
        else:
            notes.append(f"Coverage: {pct:.1f}% (last deployment {reference:.1f}%; target {COVERAGE_TARGET:.0f}% {target})")
            if pct + COVERAGE_TOLERANCE < reference:
                failures.append(f"Coverage dropped from {reference:.1f}% to {pct:.1f}%")

    lines = ["## Quality gate: " + ("FAILED" if failures else "passed"), ""]
    lines += [f"- {n}" for n in notes]
    if failures:
        lines += ["", "**Blocking:**"] + [f"- {f}" for f in failures]
    text = "\n".join(lines) + "\n"
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text)
    return 1 if failures else 0


def record(run, baseline_path):
    pct = coverage_pct(run)
    if pct is None:
        return 0
    os.makedirs(os.path.dirname(baseline_path), exist_ok=True)
    tmp = baseline_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"coverage": pct, "sha": os.environ.get("GITHUB_SHA", "")}, handle)
    os.replace(tmp, baseline_path)
    print(f"coverage reference is now {pct:.1f}%")
    return 0


if __name__ == "__main__":
    action, run_dir, baseline = sys.argv[1:4]
    sys.exit({"check": check, "record": record}[action](run_dir, baseline))
