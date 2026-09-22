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
RUN npm run pregenerate && npm run generate

# The upstream image already bundles Python (used by config/triggers/scripts/*.py),
# but not web3.py - installed here so those scripts can talk to RPCs via a proper
# ABI-encoding library instead of hand-rolled eth_call payloads.
FROM openzeppelin/openzeppelin-monitor:v1.6.0

RUN pip install --no-cache-dir --root-user-action=ignore web3

COPY --from=generator /build/config /app/config
