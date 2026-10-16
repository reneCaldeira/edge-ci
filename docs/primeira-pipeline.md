# Primeira pipeline no Edge VPS

Guia para colocar um repositório nos runners do Edge VPS usando os workflows deste repositório, do zero até o
primeiro deploy. Escrito para ser entregue a um agente de código (Codex/Claude) junto com o repositório do
projeto.

> Detalhes operacionais do host (diagnóstico, slots, broker, helper, registry) ficam num runbook privado,
> entregue a quem tem acesso ao VPS. Este guia cobre só o que vive no repositório do projeto.

## Objetivo

- **Fase 1:** CI nos runners do VPS — testes do projeto + [`quality.yml`](../.github/workflows/quality.yml)
  (substitui o Sonar).
- **Fase 2 (depois da Fase 1 verde):** build da imagem no VPS + deploy com
  [`deploy.yml`](../.github/workflows/deploy.yml), com health check e rollback.

## Como funciona

- Os jobs rodam em runners self-hosted, label **`[self-hosted, linux, x64, edge-vps]`**. Não há fallback para
  runners do GitHub: um job com `ubuntu-latest` não roda.
- O repositório precisa ser **habilitado no Snorlx** (painel de pipelines do Edge). Ao habilitar, o Snorlx cria
  a variável `EDGE_RUNNERS_ENABLED=true` no repositório e passa a registrar runners efêmeros para ele quando há
  job na fila.
- Os slots são compartilhados entre os donos, mas cada job roda como o usuário do dono do repositório
  (Docker rootless, caches e chaves próprios). Jobs nunca viram root.
- **Política de eventos:** só rodam push / `workflow_dispatch` / `schedule` na **branch padrão**, e
  `pull_request` de uma branch **do próprio repositório** para a branch padrão. Forks e `pull_request_target`
  são recusados antes do checkout.
- **Nada guardado no GitHub:** sem `actions/cache`, `upload-artifact` ou push para GHCR/Docker Hub. Caches
  ficam no HOME do runner; imagens vão para o registry local do VPS.
- **Ferramentas no runner** (use-as em vez de `setup-*` actions):

  | Ferramenta | Caminho para o `$GITHUB_PATH` |
  | --- | --- |
  | Node 26.12.0 (npm incluso) | `/opt/edge-actions/tools/node-26.12.0/bin` |
  | pnpm 12.11.2 | `/opt/edge-actions/tools/pnpm-12.11.2/bin` |
  | Go 1.27.2, govulncheck, gosec | `/opt/edge-actions/tools/go-1.27.2/bin`, `/opt/edge-actions/tools/gobin` |
  | uv 0.13.1, ruff, sqlfluff, pwsh | `/opt/edge-actions/tools/bin` |
  | Python 3.12 / 3.13 | via uv, com `UV_PYTHON_INSTALL_DIR=/opt/edge-actions/tools/python` |
  | docker (rootless), skopeo, git, curl, python3 | sistema |

## Passo 0 — no Snorlx (dono do repositório, no navegador)

1. Entrar no Snorlx com a conta GitHub (o login precisa estar liberado para ela) e aceitar os escopos do
   OAuth, inclusive **workflow**.
2. **Repositories** → sincronizar e escolher o repositório (você precisa ser admin dele).
3. **Ainda não clicar em "Enable VPS builds".** Primeiro o workflow (Passo 1): ao habilitar, o Snorlx procura
   workflows com `edge-vps` + `EDGE_RUNNERS_ENABLED`; sem isso ele recusa ou abre um PR automático
   reescrevendo jobs `ubuntu-*`.

## Passo 1 — workflow de CI

Crie `.github/workflows/edge-ci.yml` numa branch. Adapte o job `tests` à stack real do projeto (leia
`package.json`, `pyproject.toml`, `go.mod`, `Makefile`; não invente scripts):

```yaml
name: Edge CI

on:
  push:
    branches: [main]          # a branch padrão do repo
  pull_request:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: edge-ci-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

jobs:
  tests:
    name: Tests
    # só depois de habilitado no Snorlx, e só nos eventos que a política do VPS aceita
    if: >-
      vars.EDGE_RUNNERS_ENABLED == 'true' &&
      ((github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name == github.repository) ||
       (github.event_name != 'pull_request' && github.ref == 'refs/heads/main'))
    runs-on: [self-hosted, linux, x64, edge-vps]
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      # --- Node/pnpm (troque pela stack real) ---
      - name: Use the VPS Node and pnpm
        run: |
          echo "/opt/edge-actions/tools/node-26.12.0/bin" >> "$GITHUB_PATH"
          echo "/opt/edge-actions/tools/pnpm-12.11.2/bin" >> "$GITHUB_PATH"
      - run: pnpm install --frozen-lockfile
      - run: pnpm test

      # --- Python/uv ---
      # - name: Use the VPS uv
      #   run: |
      #     echo "/opt/edge-actions/tools/bin" >> "$GITHUB_PATH"
      #     echo "UV_PYTHON_INSTALL_DIR=/opt/edge-actions/tools/python" >> "$GITHUB_ENV"
      #     echo "UV_PYTHON_DOWNLOADS=never" >> "$GITHUB_ENV"
      # - run: uv sync --python 3.12 --frozen
      # - run: uv run pytest

  quality:
    name: Quality
    # o scan pesado roda na branch padrão; PRs ficam leves
    if: vars.EDGE_RUNNERS_ENABLED == 'true' && github.event_name != 'pull_request' && github.ref == 'refs/heads/main'
    uses: reneCaldeira/edge-ci/.github/workflows/quality.yml@main
    # with:
    #   exclude: "legacy/ generated/"
    #   sql-dialect: postgres
```

Recomendações:

- `runs-on` exatamente `[self-hosted, linux, x64, edge-vps]`.
- Actions de terceiros fixadas por SHA; o mínimo possível.
- Banco para testes: `docker run` no próprio job, removido com `if: always()`.
- Sem testes no projeto? Lint/build no job `tests`, e diga isso no PR.
- actionlint: declare a label em `.github/actionlint.yaml` → `self-hosted-runner: { labels: [edge-vps] }`.
- Workflows antigos com `ubuntu-*`: desligue os gatilhos ou migre-os.
- Enquanto o repositório não estiver habilitado, os jobs do PR aparecem como *skipped*: é esperado.

## Passo 2 — habilitar e disparar

1. Merge do workflow na branch padrão.
2. Snorlx → Repositories → **Enable VPS builds**.
   - Link **"Review workflow setup pull request"**: o Snorlx achou jobs `ubuntu-*` e abriu um PR reescrevendo-os.
     Revisar; na dúvida, fechar.
   - *"Sign in again to grant workflow access"* / *"runner administration is unavailable"*: **Reconnect
     GitHub** e repetir.
3. Actions → **Edge CI** → *Run workflow* na branch padrão (ou um push nela).

## Passo 3 — verificação da Fase 1

- [ ] Em até ~1 min o card de slots do Snorlx mostra `Slot N: <dono> · <dono>/<repo>`.
- [ ] O job `Tests` pega um runner `edge-...` e roda (não fica em *Queued*).
- [ ] `Quality / Quality scan` termina; o **Summary** mostra *Edge quality scan*.
- [ ] Um PR de uma branch do próprio repo roda `Tests` e pula `Quality`.
- [ ] Ao terminar, o slot fica livre de novo.

O primeiro run do `Quality` pode ficar vermelho por código antigo: sem commit anterior o repositório inteiro é
julgado. Corrija os achados reais; um push pequeno depois julga só o código novo.

## Fase 2 — deploy

Pré-condição: o app já roda no VPS como container Docker, e quem tem acesso root descreveu o app em
`/etc/edge-app-deploy/apps/<app>.json` (formato no [README](../README.md#deploy-an-app)). O helper substitui o
container existente mantendo env, volumes, portas, redes e limites; ele não cria o primeiro.

```yaml
  deploy:
    name: Deploy
    needs: [tests, quality]
    if: vars.EDGE_RUNNERS_ENABLED == 'true' && github.ref == 'refs/heads/main' && github.event_name != 'pull_request'
    uses: reneCaldeira/edge-ci/.github/workflows/deploy.yml@main
    with:
      app: <app>
      components: '[{"name": "web", "context": ".", "file": "Dockerfile"}]'
      # build-args por componente: {"name": "web", ..., "build-args": "K=V K2=V2"}
```

- `name` de cada componente = chave em `components` do JSON do app.
- O workflow põe o label `org.opencontainers.image.revision`, escaneia com Trivy (CRITICAL/HIGH bloqueiam) e
  deploya pelo digest exato.
- O Dockerfile deve ter `HEALTHCHECK`, ou o JSON do app deve ter um check HTTP.
- O build não recebe segredos; os de runtime ficam no container atual e são preservados.

Verificação:

- [ ] `Deploy` verde; o log mostra `deploy <app> <sha> web=sha256:...`.
- [ ] O container passou a usar a imagem nova, com o mesmo nome, env, volumes e portas; os containers em
      `keep_identity` não mudaram.
- [ ] Rollback: um commit que quebra o health check faz o `Deploy` falhar e o container voltar sozinho à imagem
      anterior.

## Problemas comuns

| Sintoma | Causa provável | O que fazer |
| --- | --- | --- |
| Jobs *skipped* | `EDGE_RUNNERS_ENABLED` não é `true` | Passo 2 |
| Job em *Queued* por minutos | repo não habilitado, slots ocupados ou `runs-on` diferente | card de slots no Snorlx; conferir `runs-on` |
| *"This VPS accepts only the selected repository..."* | evento fora da política | branch padrão ou PR de branch do próprio repo |
| *"No supported Linux jobs found"* ao habilitar | workflow ainda não está na branch padrão | merge primeiro |
| 403 admin ao habilitar | sem permissão de admin no repo | usar um repo onde você é admin |
| `command not found` | caminho da ferramenta não foi para o `$GITHUB_PATH` | tabela de ferramentas |
| Deploy: *unknown app* / *Invalid deployment command* | JSON do app ausente ou nomes diferentes | conferir `app` e os componentes com quem tem acesso root |
| Deploy: *revision* / *user* recusado | imagem construída fora do workflow, ou `USER` diferente do JSON | ajustar o Dockerfile ou o JSON |

## No fim, registre

1. Links do PR e dos runs; tempo de cada job.
2. Checklists acima, com o que falhou e como foi resolvido.
3. Achados do `Quality` que pareceram falso positivo (regra e arquivo).
4. Ferramentas que faltaram no runner.
5. Onde este guia estava errado ou ambíguo (abra um PR aqui).
