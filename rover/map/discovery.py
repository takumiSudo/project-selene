"""
Colony network discovery — Phase 0 + Phase 1 + Phase 2.

Responsibilities:
  - Seed the starting pod from the gateway response
  - BFS outward through /dependencies and /supplies links to find all reachable pods
  - Gap-fill by probing known hostnames on ports 3001-3012 for any pods BFS missed
  - Return a pod_registry mapping pod_id → (hostname, port) and a BFS-ordered id list

The pod_id returned by each pod's API is the Docker service name, which is also
the DNS hostname resolvable from within the rover container on selene-net.

The gateway's entrypoint URL contains "localhost" — we substitute the pod name
as the hostname since Docker DNS resolves it correctly from inside the network.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Ports assigned sequentially to the 12 habitat pods in docker-compose.yml.
# Used for port probing when a pod_id is discovered via BFS but its port is unknown.
PORT_RANGE = list(range(3001, 3013))

# Fallback hostname list for the gap-fill phase.
# In a real environment these would come from nmap / Docker DNS enumeration.
# Here they mirror the docker-compose service names exactly.
KNOWN_HOSTNAMES: list[str] = [
    "helios", "artemis", "hydroponics", "aquifer", "zephyr",
    "prometheus", "medica", "terminus", "nexus", "forge", "vault", "sentinel",
]

# How long to wait for a single probe before giving up.
PROBE_TIMEOUT = 3.0
# How long to wait for the gateway and normal pod fetches.
FETCH_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _safe_json(client: httpx.AsyncClient, url: str) -> dict | None:
    """GET url and return parsed JSON on 200, else None."""
    try:
        r = await client.get(url, timeout=FETCH_TIMEOUT)
        if r.status_code == 200:
            return r.json()
    except Exception as exc:
        logger.debug("fetch failed %s: %s", url, exc)
    return None


async def _probe_port(client: httpx.AsyncClient, hostname: str, port: int) -> int | None:
    """Return port if hostname:port responds on /info with HTTP 200, else None."""
    try:
        r = await client.get(f"http://{hostname}:{port}/info", timeout=PROBE_TIMEOUT)
        if r.status_code == 200:
            return port
    except Exception:
        pass
    return None


async def _find_port(client: httpx.AsyncClient, hostname: str) -> int | None:
    """Probe all ports in PORT_RANGE concurrently and return the first hit."""
    results = await asyncio.gather(
        *[_probe_port(client, hostname, p) for p in PORT_RANGE],
        return_exceptions=False,
    )
    for port in results:
        if port is not None:
            return port
    return None


def _extract_port_from_url(url: str) -> int | None:
    """Parse the port from a URL string like http://localhost:3002."""
    try:
        parsed = urlparse(url)
        return parsed.port
    except Exception:
        return None


def _new_pod_ids(data: dict | None, key: str) -> set[str]:
    """Extract pod_id values from a /dependencies or /supplies payload."""
    if not data:
        return set()
    return {entry["pod_id"] for entry in data.get(key, []) if "pod_id" in entry}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def discover_colony(
    gateway_url: str,
) -> tuple[dict[str, tuple[str, int]], list[str], list[str]]:
    """Discover all habitat pods reachable from the colony gateway.

    Algorithm:
      Phase 0 — seed Artemis Core from the gateway entrypoint response.
      Phase 1 — parallel BFS: at each frontier level, fetch /dependencies and
                /supplies for all frontier pods concurrently, collect newly
                referenced pod_ids, then probe their ports concurrently.
      Phase 2 — gap-fill: probe all KNOWN_HOSTNAMES on PORT_RANGE for any pods
                that BFS didn't reach (e.g. nodes with no declared relationships).

    Returns:
        pod_registry   — dict[pod_id → (hostname, port)]
        discovery_order — pod_ids in BFS visit order (gap-fill appended at end)
        unreachable    — pod_ids / hostnames that were found in the graph but
                         did not respond to any port probe
    """
    pod_registry: dict[str, tuple[str, int]] = {}
    discovery_order: list[str] = []
    unreachable: list[str] = []

    async with httpx.AsyncClient() as client:

        # ── Phase 0: seed from gateway ────────────────────────────────────
        logger.info("Phase 0: querying gateway at %s", gateway_url)
        gateway_data = await _safe_json(client, gateway_url)
        if not gateway_data:
            raise RuntimeError(f"Gateway did not respond at {gateway_url}")

        entry = gateway_data.get("entrypoint", {})
        entry_pod = entry.get("pod", "artemis")
        entry_port = _extract_port_from_url(entry.get("url", "")) or 3002

        pod_registry[entry_pod] = (entry_pod, entry_port)
        discovery_order.append(entry_pod)
        visited: set[str] = {entry_pod}
        frontier: list[str] = [entry_pod]

        logger.info("Seed: %s @ port %d", entry_pod, entry_port)

        # ── Phase 1: parallel BFS ─────────────────────────────────────────
        bfs_depth = 0
        while frontier:
            bfs_depth += 1
            logger.info("BFS depth %d — frontier: %s", bfs_depth, frontier)

            # Fetch /dependencies + /supplies for every frontier pod in parallel.
            fetch_coros = [
                asyncio.gather(
                    _safe_json(client, f"http://{pod_registry[pid][0]}:{pod_registry[pid][1]}/dependencies"),
                    _safe_json(client, f"http://{pod_registry[pid][0]}:{pod_registry[pid][1]}/supplies"),
                )
                for pid in frontier
            ]
            fetch_results = await asyncio.gather(*fetch_coros)

            # Collect all pod_ids referenced but not yet visited.
            candidates: set[str] = set()
            for dep_data, sup_data in fetch_results:
                candidates |= _new_pod_ids(dep_data, "dependencies")
                candidates |= _new_pod_ids(sup_data, "supplies")
            candidates -= visited

            if not candidates:
                logger.info("BFS complete — no new pods at depth %d", bfs_depth)
                break

            # Probe ports for all candidate pod_ids concurrently.
            port_results = await asyncio.gather(
                *[_find_port(client, pid) for pid in candidates],
                return_exceptions=False,
            )

            next_frontier: list[str] = []
            for pid, port in zip(candidates, port_results):
                visited.add(pid)
                if port is not None:
                    pod_registry[pid] = (pid, port)
                    discovery_order.append(pid)
                    next_frontier.append(pid)
                    logger.info("Discovered: %s @ port %d", pid, port)
                else:
                    unreachable.append(pid)
                    logger.warning("Pod %s referenced in graph but no port responded", pid)

            frontier = next_frontier

        # ── Phase 2: gap-fill ─────────────────────────────────────────────
        # Probe all KNOWN_HOSTNAMES to catch any pods not reachable via BFS
        # (e.g. pods that declared no deps and aren't listed in anyone's supplies).
        logger.info("Phase 2: gap-fill sweep across %d known hostnames", len(KNOWN_HOSTNAMES))
        gap_candidates = [h for h in KNOWN_HOSTNAMES if h not in pod_registry]

        if gap_candidates:
            port_results = await asyncio.gather(
                *[_find_port(client, hostname) for hostname in gap_candidates],
                return_exceptions=False,
            )
            for hostname, port in zip(gap_candidates, port_results):
                if port is not None:
                    pod_registry[hostname] = (hostname, port)
                    discovery_order.append(hostname)
                    logger.info("Gap-fill discovered: %s @ port %d", hostname, port)
                else:
                    logger.warning("Gap-fill: %s did not respond on any port", hostname)
        else:
            logger.info("Gap-fill: all known hostnames already discovered via BFS")

    logger.info(
        "Discovery complete — %d pods found, %d unreachable",
        len(pod_registry), len(unreachable),
    )
    return pod_registry, discovery_order, unreachable
