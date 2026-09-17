#!/bin/bash
input_json=$(cat)
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) MATCH: $input_json" >> /app/logs/monitor-matches.log
exit 0
