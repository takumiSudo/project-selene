#!/bin/bash
# Reporting agent entrypoint.
#
# Reads /rover/output/map.json, generates report.md with:
#   - Executive summary + risk top-3
#   - LLM-narrated crisis (with deterministic fallback)
#   - Cascade path + T+0..T+72h timeline
#   - Survivor profiles
#   - Six critical signals with verifiable citations
#   - Reconciliation summary
#   - LLM-driven recommendations (with deterministic fallback)
#   - 3 mermaid diagrams (operational graph, cascade waterfall, survivors)
#   - Methodology appendix with citation-audit summary
#
# Environment:
#   GATEWAY_URL  - colony gateway (unused by reporter; reporter reads map.json)
#   LLM_API_KEY  - optional; if absent, deterministic fallbacks are used
#   OUTPUT_DIR   - defaults to /rover/output
#
# See candidate/design_log.md Sessions 8–9.

set -euo pipefail

cd /rover
exec python3 -m report.main
