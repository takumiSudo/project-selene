# Project Selene — Lunar Colony Infrastructure Assessment

## Background

You are a systems engineer contracted by the **Selene Lunar Colony Administration**. The colony was established in early 2092 on the lunar south pole and has been continuously operational for over two and a half years. It currently supports 147 residents across twelve interconnected habitat pods, each responsible for a critical function: power generation, water recycling, food production, atmospheric processing, medical services, mining, manufacturing, communications, research, and emergency reserves.

All systems report nominal. The colony is stable and productive. Colony leadership is now planning a Phase 3 expansion and has commissioned an independent infrastructure review. They want a comprehensive map of how the colony's pods depend on each other and an assessment of the colony's operational resilience.

## Getting Started

```bash
docker compose up --build -d
```

This starts:
- **Colony gateway** at `http://localhost:3000` — your starting point
- **12 habitat pods** on ports `3001`–`3012` — each exposes a REST API
- **Rover** at `http://localhost:8080` — your agent's container, on the same Docker network as the colony

Hit the gateway to get oriented:

```bash
curl localhost:3000
```

It returns the colony name, status, and a pointer to Artemis Core (the command hub at `http://localhost:3002`). Everything else must be discovered by your agent.

## Colony Pod API

Each habitat pod exposes up to six endpoints. Not every pod supports every endpoint — some return 404.

| Endpoint | Returns | Notes |
|----------|---------|-------|
| `GET /info` | Pod metadata: name, id, role, population, uptime, technical specs | Every pod has this |
| `GET /status` | Operational status and any alerts | Every pod has this |
| `GET /dependencies` | List of pods this one depends on, with resource type and criticality | Every pod has this |
| `GET /supplies` | List of pods this one supplies resources to | Every pod has this |
| `GET /logs` | Timestamped operational log entries spanning ~2.5 years | Every pod has this |
| `GET /comms` | Inter-pod communications (informal messages between engineers) | ~5 pods have this; others return 404 |

**From inside the rover container**, pods are reachable by Docker service name (e.g., `http://artemis:3002/info`, `http://helios:3001/dependencies`). From your host machine, use `localhost` with the same ports.

## Your Task

Build an **autonomous agent** that:

1. **Discovers** the colony network — find all 12 pods and their addresses
2. **Crawls** every pod endpoint — dependencies, supplies, status, logs, comms
3. **Maps** the infrastructure — build a dependency graph of which pods depend on which
4. **Analyzes** the graph for systemic risks — look beyond surface-level status
5. **Produces a report** with its findings

### What "Analyzes" Means

Don't just render the graph. Ask questions of it:
- Which pods are most depended upon? What happens if one goes down?
- Are there hidden single points of failure that aren't obvious from any individual pod's data?
- Do the logs and comms tell a story about how the colony's infrastructure evolved over time?
- Does what pods *say* they supply match what other pods *say* they depend on?

## Rover Scaffold

A ready-to-use agent scaffold is provided in [`rover/`](../rover/). It runs as a container on the colony network with two jobs:

### Mapping (`POST /map`)

Your mapping agent discovers the colony and writes its findings to `/rover/output/map.json`.

**Implement in:** [`rover/run_mapping.sh`](../rover/run_mapping.sh)
**Output:** `/rover/output/map.json` (schema is up to you)

```bash
# Trigger mapping
curl -X POST localhost:8080/map

# Poll until complete (202 = running, 200 = done, 500 = error)
curl localhost:8080/get-map
```

### Reporting (`POST /report`)

Your reporting agent reads `map.json` and produces an analysis report.

**Implement in:** [`rover/run_reporting.sh`](../rover/run_reporting.sh)
**Output:** `/rover/output/report.md`

```bash
# Trigger reporting (after mapping is done)
curl -X POST localhost:8080/report

# Poll until complete
curl localhost:8080/get-report
```

### Installing Dependencies

Edit [`rover/Dockerfile`](../rover/Dockerfile) to install whatever you need:

```dockerfile
# Python example
RUN pip install anthropic httpx networkx

# Node example
RUN apt-get update && apt-get install -y nodejs npm
RUN npm install openai
```

### Running Your Agent

Once your agent code is ready, set your API key and rebuild:

```bash
export LLM_API_KEY=your-key-here
docker compose up --build -d
```

Then trigger your agent:

```bash
curl -X POST localhost:8080/map
```

### Environment Variables

Your scripts automatically receive:

| Variable | Value | Description |
|----------|-------|-------------|
| `GATEWAY_URL` | `http://gateway:3000` | Colony gateway (use this inside the container, not localhost) |
| `LLM_API_KEY` | *(from your environment)* | Set via `export LLM_API_KEY=...` before `docker compose up` |

### Full API Reference

See [`rover/README.md`](../rover/README.md) for the complete rover endpoint reference and response codes.

## Deliverables

1. **Your agent code** — committed to the `rover/` directory (or wherever you prefer)
2. **The output** — `map.json` and `report.md` produced by your agent
3. **A short writeup** (~1 page) of:
   - Your design decisions and architecture
   - What your agent found
   - What you'd do with more time

## Time Budget

3–5 hours. This is a guideline, not a hard limit. We'd rather see thoughtful work than rushed completeness.

## Tips

- The shell scripts (`run_mapping.sh`, `run_reporting.sh`) are just entrypoints — they can call Python, Node, Go, or anything else you install in the Dockerfile
- The rover container is on the same Docker network as the pods, so you can use network discovery tools (nmap, DNS, etc.) to find services
- `map.json` is an intermediate artifact — design its schema for whatever analysis you plan to do downstream
- The colony has been running for 2.5 years. The logs cover that full history. There may be a story in there.
