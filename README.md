# sf-monitor

Monitors SecuredFinance's on-chain smart contracts for critical safety events (liquidations, auto-rolls, itayose, block-unit-price fluctuations, large TokenVault withdrawals) and alerts the team when they fire.

This is a config-driven deployment of [OpenZeppelin Monitor](https://github.com/OpenZeppelin/openzeppelin-monitor) — we don't fork or build the monitor itself, only author the JSON config (and a few Python trigger scripts) that tell it which networks/contracts/events to watch and where to send alerts.

## Prerequisites

- Node (see `.nvmrc` for the pinned version)
- Docker and Docker Compose
- A GitHub personal access token with read access to the private `@secured-finance/contracts` npm package

## Setup

1. Install dependencies:

   ```sh
   npm install
   ```

2. Copy `.env.example` to `.env` and fill in the values (RPC URLs per network, `SLACK_WEBHOOK_URL`, `NPM_AUTH_TOKEN`, `SF_ENV`):

   ```sh
   cp .env.example .env
   ```

3. Generate the monitor config for your target environment:

   ```sh
   SF_ENV=development npm run generate
   ```

   This resolves contract addresses for that environment/chain from the private `@secured-finance/contracts` package (and, for a couple of concerns, live via RPC) and writes `config/monitors/<network>-<concern>.json`. Only one environment's monitors exist on disk at a time — re-run with a different `SF_ENV` to switch.

## Running locally

```sh
docker compose up
```

This builds the monitor image and starts it with `config/`, `data/`, and `logs/` mounted from the repo. `data/` holds the per-network last-processed-block cursor and any per-concern script state; `logs/monitor-matches.log` records every match.

## Available scripts

| Script | Description |
| --- | --- |
| `npm run generate` | Generate `config/monitors/*.json` for the `SF_ENV` set in the environment |
| `npm run pregenerate` | Remove all previously generated monitor files |
| `npm run typecheck` | Type-check the generator (`tools/`) |
| `npm run lint:check` / `lint:fix` | Lint the generator |
| `npm run prettier:check` / `prettier:fix` | Check/fix formatting |

## Project layout

- `config/networks/` — per-network RPC/chain definitions
- `config/monitors/` — generated (gitignored) and hand-written monitor definitions
- `config/triggers/` — alert destinations (Slack, local logging, custom scripts)
- `tools/` — the monitor generator (see `tools/concerns/` for one module per watched condition)
- `data/`, `logs/` — runtime state and match logs (gitignored)

See [CLAUDE.md](CLAUDE.md) for the current status of each monitored concern, known gaps, and the reasoning behind non-obvious design decisions (e.g. why some concerns resolve their watched contract address live via RPC instead of a static address).

## Deployment

Deploys are manual only — run the `Deploy sf-monitor to GKE` workflow (`.github/workflows/monitor-gke-deploy.yml`) via `workflow_dispatch` and pick `development`, `staging`, or `production`. There's no push-triggered deploy: development/staging exist for testing only, and production deploys are deliberate rather than tied to a merge.
