# sf-monitor

Monitors SecuredFinance's on-chain smart contracts for critical safety events, alerting the team when they fire.

## What this is

A config-driven deployment of [OpenZeppelin Monitor](https://github.com/OpenZeppelin/openzeppelin-monitor) (`openzeppelin/openzeppelin-monitor` Docker image). We don't fork or build the monitor itself — we only author JSON config that tells it which networks/contracts/events to watch and where to send alerts. Runs via `docker-compose.yml`.

## Status

Migrated off OpenZeppelin Defender (Sentinel + Autotasks, deprecated) onto this self-hosted setup. The previous Defender-based TypeScript deployment code (`old-definitions/`) has been removed now that the concerns it covered are ported; block-unit-price-fluctuations below is being designed fresh rather than ported from it.

- **LendingMarketController liquidations/auto-rolls/itayose, and TokenVault total-deposits: live**, generated (not hand-written) by `npm run generate` (`tools/generate-monitors.ts`), one concern module per condition under `tools/concerns/`. Set `SF_ENV` to `development`, `staging`, or `production` in `.env` and run it — it resolves that environment's contract address per chain dynamically from the private `@secured-finance/contracts` npm package (`tools/deployments.ts`) and writes `config/monitors/<network>-<concern>.json`. Only one environment's monitors exist on disk/deployed at a time (`npm run pregenerate` wipes the previous set first) — these generated files are gitignored, not committed; regenerate per environment instead.
- **Total-deposits (large-withdrawal alert logic): live.** `tools/concerns/total-deposits.ts` watches TokenVault's `Withdraw` event and fires the `sf_monitor_total_deposits_check` script trigger (`config/triggers/scripts.json` → `config/triggers/scripts/total_deposits_check.py`), which resolves the withdrawn currency's token address live via `TokenVault.getTokenAddress` (web3.py, installed via this repo's `Dockerfile`) rather than a static currency→address map. Still open: no test validating the real OZ Monitor match-payload schema against the script's parsing.
- **TokenVault large-withdrawal monitor (the old hand-written one): removed for now.** It previously lived at `config/monitors/ethereum_sepolia_tokenvault.json` (hand-written, not generated) but was deleted while restructuring around the generator — superseded by the total-deposits concern above, which covers the same contract via the generator instead.
- **block-unit-price-fluctuations: not started.**
- Filecoin not yet configured.

## Layout

- `config/networks/` — RPC/chain definitions per network (`slug` must match what monitors reference in their `networks` array: `ethereum_mainnet`, `ethereum_sepolia`, `arbitrum_one`, `arbitrum_sepolia`)
- `config/monitors/` — one file per contract/event set being watched (address, ABI fragment, match conditions, which triggers to fire). Files matching `*-liquidations.json`/`*-autorolls.json`/`*-itayose.json`/`*-totaldeposits.json` are generated (gitignored, see `tools/`) — everything else here is hand-written and committed.
- `config/filters/` — custom filter scripts, if any
- `config/triggers/` — alert destinations (`slack.json` declarative alerts, `log.json` local logging, `scripts.json` + `scripts/` for monitors needing custom computation/state)
- `data/` — per-network last-processed-block cursor (gitignored)
- `logs/` — monitor match logs (gitignored)
- `.env` — RPC URLs, `SLACK_WEBHOOK_URL`, `NPM_AUTH_TOKEN`, `SF_ENV` (gitignored; see `.env.example`)
- `tools/` — the monitor generator (Node/TypeScript, `npm run generate`). `environments.ts` maps each `SF_ENV` to the chains/deploy-stages it covers, `deployments.ts` dynamically resolves each chain's contract address from the private `@secured-finance/contracts` npm package (auth via `NPM_AUTH_TOKEN`), `concerns/*.ts` define one LendingMarketController-watched condition each (event ABI, signature, Slack trigger id), `generate-monitors.ts` ties them together and writes `config/monitors/`.

## Working principles

- **Minimal code changes**, but not at the cost of hardcoding what has a real dynamic source. OZ Monitor's own config format is static (no env-var templating for contract addresses), but contract addresses/ABI fragments still come from the private `@secured-finance/contracts` package dynamically via `tools/generate-monitors.ts` — never hand-type an address into a monitor JSON file if the generator can resolve it.
- Only one `SF_ENV` (development/staging/production) is ever deployed on a given monitor instance at a time — the generator reflects that: `npm run generate` regenerates and replaces the full LendingMarketController-monitor set for whichever `SF_ENV` is set, and those generated files are gitignored rather than committed.
- New watched conditions go in `tools/concerns/` (event ABI + signature + trigger), not as hand-written JSON — see `tools/concerns/liquidations.ts` for a Slack-triggered pattern and `tools/concerns/total-deposits.ts` for a script-triggered one. Hand-written monitor JSON is still the right approach for contracts/conditions the generator doesn't cover yet (nothing currently — block-unit-price-fluctuations hasn't been started, generated or otherwise).
- Contract addresses and events being monitored are security-critical — changes here should be deliberate and reviewed, since a misconfigured monitor means missed alerts on real protocol risk.
