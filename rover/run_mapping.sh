#!/bin/bash
# Mapping phase entrypoint.
# Delegates to rover/map/main.py which runs five phases:
#   Phase 0  discover   BFS from gateway
#   Phase 1  crawl      all 6 endpoints per pod
#   Phase 2  reconcile  cross-ref /dependencies vs /supplies (sanity check)
#   Phase 3  timeline   merge + sort all logs and comms
#   Phase 4  metrics    structural + operational + LLM enrichment
#   Phase 5  assemble   write map.json
#
# Phase artefacts are written to OUTPUT_DIR/phases/ for debugging.
# Final output: OUTPUT_DIR/map.json
#
# Environment variables:
#   GATEWAY_URL   default: http://gateway:3000
#   LLM_API_KEY   optional — Layer C (LLM enrichment) is skipped if not set
#   OUTPUT_DIR    default: /rover/output

set -euo pipefail

export PYTHONUNBUFFERED=1
export OUTPUT_DIR="${OUTPUT_DIR:-/rover/output}"
export GATEWAY_URL="${GATEWAY_URL:-http://gateway:3000}"

mkdir -p "${OUTPUT_DIR}/phases"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║         SELENE MAPPING AGENT                 ║"
echo "╚══════════════════════════════════════════════╝"
echo "  Gateway:  ${GATEWAY_URL}"
echo "  Output:   ${OUTPUT_DIR}"
if [ -n "${LLM_API_KEY:-}" ]; then
  echo "  LLM key:  configured (Layer C enabled)"
else
  echo "  LLM key:  not set — Layer C will be skipped"
fi
echo ""

cd /rover
exec python -m map.main
