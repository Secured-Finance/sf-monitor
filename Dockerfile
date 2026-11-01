# Bakes one SF_ENV's monitor config into the image (config/monitors/*.json is gitignored -
# generated fresh here, same as `npm run generate` locally, since a deployed container has no
# bind mount to fall back on the way docker-compose does).
FROM node:22-alpine AS generator
ARG SF_ENV
WORKDIR /build
COPY package.json package-lock.json .npmrc tsconfig.json ./
COPY tools/ ./tools/
COPY config/ ./config/
RUN --mount=type=secret,id=npm_auth_token,env=NPM_AUTH_TOKEN \
    npm ci
# block-unit-price/itayose resolve their watched addresses via a live RPC call at
# generate time (tools/concerns/lending-market-addresses.ts) - the generator has never
# needed RPC access before this, so these are passed the same way npm_auth_token is:
# build-time secrets, never baked into an image layer. All six are always passed here
# (mirroring the "Create Kubernetes secrets" step in
# .github/workflows/monitor-gke-deploy.yml, which creates them all regardless of
# environment too) even though a given SF_ENV's ENV_CHAINS (tools/environments.ts) only
# uses three of them.
#
# The two Filecoin ones are the real upstream endpoints, not the adapter's loopback URL
# that config/networks/filecoin_*.json names: no adapter is listening during a build,
# so tools/rpc.ts deliberately overrides those networks to use these instead.
RUN --mount=type=secret,id=ethereum_mainnet_rpc_url,env=ETHEREUM_MAINNET_RPC_URL \
    --mount=type=secret,id=ethereum_sepolia_rpc_url,env=ETHEREUM_SEPOLIA_RPC_URL \
    --mount=type=secret,id=arbitrum_one_rpc_url,env=ARBITRUM_ONE_RPC_URL \
    --mount=type=secret,id=arbitrum_sepolia_rpc_url,env=ARBITRUM_SEPOLIA_RPC_URL \
    --mount=type=secret,id=filecoin_mainnet_rpc_url,env=FILECOIN_MAINNET_RPC_URL \
    --mount=type=secret,id=filecoin_calibration_rpc_url,env=FILECOIN_CALIBRATION_RPC_URL \
    npm run pregenerate && npm run generate

# The upstream image already bundles Python (used by config/triggers/scripts/*.py),
# but not web3.py - installed here so those scripts can talk to RPCs via a proper
# ABI-encoding library instead of hand-rolled eth_call payloads.
FROM openzeppelin/openzeppelin-monitor:v1.6.0

RUN pip install --no-cache-dir --root-user-action=ignore web3

COPY --from=generator /build/config /app/config

# The Filecoin null-round bridge. Not an OZ Monitor trigger script - it runs as its own
# process (a sidecar container in GKE, a separate service in docker-compose) that the
# monitor reaches over loopback, so it lives outside config/ and is copied in directly.
COPY adapters/ /app/adapters/
