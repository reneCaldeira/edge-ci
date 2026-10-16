# edge-ci

Shared CI for repositories built on the Edge VPS runners. It replaces SonarQube with scanners that run on the VPS
itself and judges **new code** only, the way Sonar's "Sonar way" quality gate does.

## Use it

Add one job to the repository's workflow (main pushes; pull requests stay light):

```yaml
jobs:
  quality:
    if: github.event_name != 'pull_request'
    uses: reneCaldeira/edge-ci/.github/workflows/quality.yml@main
    # with:
    #   exclude: "legacy/ generated/"   # extra paths or globs to skip
    #   sql-dialect: postgres            # for .sql files
```

The job runs only on the Edge VPS runner pool (`self-hosted, linux, x64, edge-vps`); enable the repository in
Snorlx first.

## What runs

Languages are detected from the repository's files; a tool runs only when its language is present.

| Tool | Covers | Blocks |
| --- | --- | --- |
| OpenGrep + community rules ([`opengrep/collect_rules.py`](opengrep/collect_rules.py)) | vulnerabilities and secrets in Python, JS/TS, shell, Dockerfile, YAML/workflows, HTML, JSON, PHP, Ruby | new findings with severity ERROR and HIGH confidence |
| jscpd | duplication, any language | more than 10% of the lines in changed files |
| Trivy (filesystem) | vulnerable dependencies, misconfiguration | CRITICAL/HIGH |
| ruff | Python bugs and code smells (the repository's ruff config wins) | report only |
| shellcheck, hadolint, actionlint | shell scripts, Dockerfiles, GitHub workflows | report only |
| PSScriptAnalyzer | PowerShell | report only |
| sqlfluff | SQL (`sql-dialect`, default postgres) | report only |

Not scanned by decision: Go and Liquid templates.

"New code" is the diff from the branch's previous commit (`github.event.before`); a manual run compares with the
last deployment that passed the gate. Without either, the whole repository is judged.

## Deployment gate

Results also land in `~/.cache/quality/<owner>/<repo>/<run id>/` on the runner (shared by the runner slots of the
same owner). A later job of the same run can add its own results (`coverage-summary.json` from Vitest,
`sonarjs.json` from eslint-plugin-sonarjs) and decide with:

```bash
python3 .edge-ci/quality/gate.py check "$HOME/.cache/quality/$GITHUB_REPOSITORY/$GITHUB_RUN_ID" \
  "$HOME/.cache/quality/$GITHUB_REPOSITORY/baseline.json"
# after a successful deployment, the run's coverage becomes the reference:
python3 .edge-ci/quality/gate.py record ...same arguments...
```

Coverage may not drop below the last deployment (target 60% is reported); sonarjs bugs in changed files block.

## Rules

`opengrep/collect_rules.py` fetches pinned commits of
[semgrep/semgrep-rules](https://github.com/semgrep/semgrep-rules) (Semgrep Rules License v1.0, internal use) and
[trailofbits/semgrep-rules](https://github.com/trailofbits/semgrep-rules) (AGPL-3.0) into the runner's cache and
assembles one rule file, without "audit" rules and without Go, Liquid and Terraform. The rules are not stored in
this repository. Bump a pin to take newer rules.

## Deploy an app

`deploy/edge_deploy.py` is a generic version of the Snorlx deployment helper. The CI builds the images on the
runner, scans them, pushes them to the VPS's local registry (`127.0.0.1:5000`) and asks the helper to deploy the
exact digests; the helper replaces the app's containers, keeps their whole runtime (environment, mounts, ports,
networks, limits), waits for health and rolls every container back on failure.

```yaml
  deploy:
    needs: [tests, quality]
    if: github.ref == 'refs/heads/main' && github.event_name != 'pull_request'
    uses: reneCaldeira/edge-ci/.github/workflows/deploy.yml@main
    with:
      app: contratos
      components: '[{"name": "web", "context": ".", "file": "Dockerfile"}]'
```

Each app is described once on the VPS, root-owned, in `/etc/edge-app-deploy/apps/<app>.json`:

```json
{
  "owner": "fespadoni",
  "components": {
    "web": {"container": "sante-contratos-contracts-1", "repository": "fespadoni/contratos-web", "user": "1000"}
  },
  "keep_identity": ["sante-contratos-db-1"],
  "checks": [{"url": "http://127.0.0.1:18080/health", "status": 200}],
  "health_timeout": 180
}
```

- `owner`: only that GitHub owner's runner user can deploy the app (its SSH key's forced command fixes the owner).
- `components.*.container`: the running container that is replaced; it must already exist (the first start is
  done once by hand or by compose). `repository` is `<owner>/<app>-<component>` in the local registry. `user`
  (optional) is the runtime user the image must declare. `health`: `docker` (default; waits for the container's
  health check when it has one) or `running`.
- `keep_identity`: containers that must still be the same afterwards (databases).
- `checks`: HTTP checks on `127.0.0.1` that must answer the given status.

Refused before anything stops: an image whose `org.opencontainers.image.revision` is not the commit, a
privileged container, host networking, a Docker socket mount. Install or update the helper with
`sudo bash deploy/install.sh` on the VPS.
