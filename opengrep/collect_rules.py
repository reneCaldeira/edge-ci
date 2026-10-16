#!/usr/bin/env python3
"""Assemble the OpenGrep rule set from pinned community repositories into one directory of rule files.

Each source is fetched once at its pinned commit into the runner's cache and reused offline afterwards;
changing a pin or the filters below is the only thing that rebuilds the set. Kept: the languages our and
Fernando's code is written in, without "audit" rules (review prompts, not vulnerabilities: they produced
false blocking findings and doubled the scan time).

usage: collect_rules.py <cache-dir>   (prints the rule file to pass to `opengrep scan --config`)

All rules go into one file: loading ~800 rules from hundreds of files took 409 s, one file takes 35 s.
"""
import hashlib
import os
import shutil
import subprocess
import sys

import yaml

# (name, repository, commit, licence) - bump a commit to take newer rules.
SOURCES = [
    ("semgrep-rules", "https://github.com/semgrep/semgrep-rules",
     "17e31e11ebac99fb7dfe1cd288f7a32151b6f3f9", "Semgrep Rules License v1.0 (internal use)"),
    ("trailofbits", "https://github.com/trailofbits/semgrep-rules",
     "31390b3a99c04c81522d1b37c8d1900aa2dd4094", "AGPL-3.0"),
]
# Top-level rule directories kept. Not here on purpose (2026-10-16): Go (only Snorlx, rarely edited), Liquid
# (Loja Integrada templates), Terraform and languages nobody writes (Java is only the stock Metabase image).
LANGUAGES = {"python", "javascript", "typescript", "generic", "dockerfile", "yaml", "bash", "html", "json", "php",
             "ruby", "ai", "problem-based-packs"}
SKIP_DIRS = {".git", ".github", "scripts", "stats", "tests", "test", "fixtures"}


def fetch(cache, name, url, commit):
    target = os.path.join(cache, "src", f"{name}-{commit[:12]}")
    if os.path.isdir(os.path.join(target, ".git")):
        return target
    tmp = target + ".partial"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    git = ["git", "-C", tmp]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["fetch", "-q", "--depth", "1", url, commit], check=True)
    subprocess.run(git + ["checkout", "-q", "FETCH_HEAD"], check=True)
    os.rename(tmp, target)
    return target


def kept_rules(path):
    if path.endswith((".test.yaml", ".test.yml", ".fixed.yaml")):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            doc = yaml.safe_load(handle)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return []
    if not isinstance(doc, dict) or not isinstance(doc.get("rules"), list):
        return []
    out = []
    for rule in doc["rules"]:
        if not isinstance(rule, dict):
            continue
        sub = (rule.get("metadata") or {}).get("subcategory") or []
        if "audit" in ([sub] if isinstance(sub, str) else sub) or "/audit/" in path.replace(os.sep, "/"):
            continue
        out.append(rule)
    return out


def main():
    cache = sys.argv[1]
    key = hashlib.sha256(repr((SOURCES, sorted(LANGUAGES))).encode()).hexdigest()[:12]
    out = os.path.join(cache, f"ruleset-{key}.yml")
    if not os.path.exists(out):
        rules = []
        for name, url, commit, _ in SOURCES:
            source = fetch(cache, name, url, commit)
            for top in sorted(os.listdir(source)):
                if top not in LANGUAGES:
                    continue
                for root, dirs, files in os.walk(os.path.join(source, top)):
                    dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
                    for f in sorted(files):
                        if f.endswith((".yaml", ".yml")):
                            rules += kept_rules(os.path.join(root, f))
        tmp = out + ".partial"
        with open(tmp, "w", encoding="utf-8") as handle:
            yaml.safe_dump({"rules": rules}, handle, sort_keys=False)
        os.replace(tmp, out)
        print(f"assembled {len(rules)} rules into {out}", file=sys.stderr)
    print(out)


if __name__ == "__main__":
    main()
