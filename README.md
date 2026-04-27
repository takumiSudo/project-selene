# Project Selene

## Infrastructure Assessment Write Up 

**→ [`candidate/README.md`](candidate/README.md) — the writeup**

TL;DR: the colony is 54 hours from a life-critical failure if Aquifer goes down and only 2 of 12 pods survive. Read the writeup for the full OKRs, design decisions, cascade diagram, and recommendations.

---

## Repo Structure

```
candidate/
  README.md                          ← writeup (start here)
  deliverable/
    artifact/
      map.json                       ← canonical colony map (12 pods, 39 edges, 168 KB)
      report.md                      ← full assessment report with mermaid diagrams
    canonical_design_doc.md          ← all architecture decisions across 10 sessions
    Assessment_Instructions.md       ← original problem brief

rover/
  map/                               ← mapping agent (BFS discovery, crawler, metrics)
  report/                            ← reporting agent (deterministic + LLM sections)
  run_mapping.sh                     ← mapping entrypoint
  run_reporting.sh                   ← reporting entrypoint
  Dockerfile

configs/                             ← 12 pod configuration files
pod-service/                         ← pod REST API server
gateway/                             ← colony gateway
docker-compose.yml
Makefile
```

---

## Running the Agent

```bash
echo "LLM_API_KEY=sk-ant-..." >> .env   # optional — deterministic fallbacks used if absent
make run                                 # build → start colony → map → report → print
```

Outputs land in `.artifacts/` and are duplicated to `candidate/deliverable/artifact/`.
See `candidate/README.md → How to Run` for individual phase commands.
