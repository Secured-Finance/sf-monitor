# The upstream image already bundles Python (used by config/triggers/scripts/*.py),
# but not web3.py - installed here so those scripts can talk to RPCs via a proper
# ABI-encoding library instead of hand-rolled eth_call payloads.
FROM openzeppelin/openzeppelin-monitor:v1.6.0

RUN pip install --no-cache-dir --root-user-action=ignore web3
