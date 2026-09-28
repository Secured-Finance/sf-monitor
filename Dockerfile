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
# build-time secrets, never baked into an image layer. All four are always passed here
# (mirroring the "Create Kubernetes secrets" step in
# .github/workflows/monitor-gke-deploy.yml, which creates all four regardless of
# environment too) even though a given SF_ENV's ENV_CHAINS (tools/environments.ts) only
# uses two of them.
RUN --mount=type=secret,id=ethereum_mainnet_rpc_url,env=ETHEREUM_MAINNET_RPC_URL \
    --mount=type=secret,id=ethereum_sepolia_rpc_url,env=ETHEREUM_SEPOLIA_RPC_URL \
    --mount=type=secret,id=arbitrum_one_rpc_url,env=ARBITRUM_ONE_RPC_URL \
    --mount=type=secret,id=arbitrum_sepolia_rpc_url,env=ARBITRUM_SEPOLIA_RPC_URL \
    npm run pregenerate && npm run generate

# The upstream image already bundles Python (used by config/triggers/scripts/*.py),
# but not web3.py - installed here so those scripts can talk to RPCs via a proper
# ABI-encoding library instead of hand-rolled eth_call payloads.
FROM openzeppelin/openzeppelin-monitor:v1.6.0

RUN pip install --no-cache-dir --root-user-action=ignore web3

COPY --from=generator /build/config /app/config
