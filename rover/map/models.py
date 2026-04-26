from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Criticality(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class EdgeState(str, Enum):
    """Reconciliation outcome for a dependency edge."""
    RECONCILED   = "reconciled"    # both /dependencies and /supplies declared it
    DEP_ONLY     = "dep_only"      # source claims dep; target's /supplies omits it
    SUPPLY_ONLY  = "supply_only"   # target claims supply; source's /deps omits it


class MetadataSignalType(str, Enum):
    NO_BACKUP          = "no_backup"           # backup field is 0/null/false
    IMPLIED_DEPENDENCY = "implied_dependency"   # field value references another pod
    RESILIENCE_MARKER  = "resilience_marker"    # pod has independent resource capability
    STALE_REFERENCE    = "stale_reference"      # field value contradicts a log-dissolved edge


class LLMRelationshipType(str, Enum):
    """Structured relationship types the LLM can emit. Maps to a directed edge."""
    IMPLICIT_DEPENDENCY  = "implicit_dependency"   # A depends on B (not formally declared)
    STALE_DEPENDENCY     = "stale_dependency"       # declared dep is no longer operationally accurate
    RELIABILITY_CONCERN  = "reliability_concern"    # A expressed worry about B's availability
    UNDECLARED_SUPPLY    = "undeclared_supply"       # B supplies A (not in formal /supplies)
    CAPACITY_RISK        = "capacity_risk"           # A is approaching an operational limit affecting B


# ---------------------------------------------------------------------------
# Raw pod API response shapes
# ---------------------------------------------------------------------------


class ResourceLink(BaseModel):
    """One entry from a pod's /dependencies or /supplies list."""
    pod_id: str
    resource: str
    criticality: Criticality = Criticality.UNKNOWN
    notes: str = ""


class PodInfo(BaseModel):
    """Payload from GET /info."""
    id: str
    name: str
    role: str
    population: int = 0
    status: str = ""
    uptime_days: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class PodStatus(BaseModel):
    """Payload from GET /status."""
    status: str
    alerts: list[str] = Field(default_factory=list)
    last_incident: str | None = None


class LogEntry(BaseModel):
    """One timestamped event from a pod's /logs, enriched with pod origin."""
    kind: Literal["log"] = "log"
    timestamp: datetime
    pod_id: str
    pod_name: str = ""
    event: str      # e.g. "commissioning", "maintenance", "directive"
    detail: str


class CommEntry(BaseModel):
    """One inter-pod message from a pod's /comms endpoint.

    sender/recipient are role identifiers like "artemis_ops", NOT pod_ids.
    pod_id is the pod whose /comms endpoint we crawled.
    """
    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["comm"] = "comm"
    timestamp: datetime
    pod_id: str = ""
    pod_name: str = ""
    sender: str
    recipient: str
    content: str


TimelineEvent = Annotated[Union[LogEntry, CommEntry], Field(discriminator="kind")]


# ---------------------------------------------------------------------------
# Computed graph structures — Layer A (structural)
# ---------------------------------------------------------------------------


class DependencyEdge(BaseModel):
    """Directed edge: source depends on target for a given resource."""
    source: str
    target: str
    resource: str
    criticality: Criticality = Criticality.UNKNOWN
    declared_by_source: bool = False
    declared_by_target: bool = False
    reconciled: bool = False
    state: EdgeState = EdgeState.DEP_ONLY

    def finalise(self) -> DependencyEdge:
        self.reconciled = self.declared_by_source and self.declared_by_target
        if self.reconciled:
            self.state = EdgeState.RECONCILED
        elif self.declared_by_source:
            self.state = EdgeState.DEP_ONLY
        else:
            self.state = EdgeState.SUPPLY_ONLY
        return self


class BlastRadiusEntry(BaseModel):
    """Transitive blast radius for one pod — everything that fails if this pod fails."""
    pod_id: str
    blast_radius_pods: list[str] = Field(default_factory=list)
    blast_radius_count: int = 0
    # Sum of edge criticality weights across the entire induced failure subgraph.
    blast_radius_weighted: float = 0.0


class CascadeStep(BaseModel):
    """One step in a simulated failure cascade."""
    pod_id: str
    failure_mode: str                        # e.g. "loses electrical_power from helios"
    hop: int = 0                             # 1 = direct dep, 2+ = transitive
    estimated_window_hours: float | None = None  # this pod's own buffer for the lost resource
    cumulative_hours: float | None = None    # min time from T=0 (trigger) until this pod fails
    evidence_source: str = ""               # e.g. "metadata:backup_power_hours=4"
    is_life_critical: bool = False          # oxygen, power, medical resources
    is_compound: bool = False               # pod is in same SCC as trigger (mutual destruction)
    immediate_supplier: str = ""             # which upstream pod provides the lost resource
    lost_resource: str = ""                 # explicit resource name


class CascadeSurvivor(BaseModel):
    """A pod that survives a cascade either by independence or by sufficient buffer."""
    pod_id: str
    reason: str                              # e.g. "independent — no operational path"
    evidence: list[str] = Field(default_factory=list)  # metadata + structural evidence


class CascadeSimulation(BaseModel):
    """Full failure cascade rooted at a corroborated SPOF."""
    trigger_pod: str
    trigger_reason: str
    corroboration_signals: list[str] = Field(default_factory=list)  # why this pod was selected
    direct_dependents: list[str] = Field(default_factory=list)       # hop=1 pods (operational)
    compound_dependents: list[str] = Field(default_factory=list)     # in trigger's SCC (mutual)
    steps: list[CascadeStep] = Field(default_factory=list)
    survivors: list[CascadeSurvivor] = Field(default_factory=list)
    total_pods_affected: int = 0
    time_to_life_critical_hours: float | None = None  # fastest path to life-critical failure
    time_to_colony_wide_hours: float | None = None    # max cumulative — when everyone has fallen


# ---------------------------------------------------------------------------
# Computed structures — Layer B (operational / metadata + logs)
# ---------------------------------------------------------------------------


class MetadataSignal(BaseModel):
    """An implicit dependency or resilience marker extracted from pod metadata."""
    pod_id: str
    field: str
    value: Any
    signal_type: MetadataSignalType
    implied_dep_target: str | None = None    # pod_id implied as a dependency target
    implied_dep_resource: str | None = None
    description: str = ""


class HistoricalEdge(BaseModel):
    """A dependency that existed operationally but was formally dissolved.

    Detected by scanning the timeline for dissolution keywords.
    still_declared=True means the formal dep graph is stale (not updated after dissolution).
    """
    source: str              # pod that had (or has) the dependency
    target: str              # pod that was (or is) the supplier
    resource: str
    dissolved_at: datetime
    log_pod: str             # which pod's log contained the evidence
    evidence_text: str
    still_declared: bool = False   # True if formal /dependencies still lists this


class RiskScore(BaseModel):
    """Composite risk score for one pod across all signal layers."""
    pod_id: str
    blast_radius_score: float = 0.0      # normalised 0–1 (fraction of colony affected)
    vulnerability_score: float = 0.0     # normalised 0–1 (transitive upstream dep depth)
    spof_corroboration_count: int = 0    # independent signals confirming SPOF status
    is_articulation_point: bool = False
    is_metadata_spof: bool = False       # metadata has backup=0 or equivalent
    historical_dissolution_count: int = 0
    overall_risk: float = 0.0            # weighted composite


# ---------------------------------------------------------------------------
# Computed structures — Layer C (LLM enrichment)
# ---------------------------------------------------------------------------


class LLMDerivedSignal(BaseModel):
    """A structured dependency signal extracted by the LLM from comms and logs.

    Always represents a directed relationship:  source_pod → target_pod
    with a typed relationship and the verbatim evidence quote.
    """
    source_pod: str
    target_pod: str
    relationship_type: LLMRelationshipType
    resource: str | None = None
    is_formally_declared: bool = False
    confidence: float                          # 0.0–1.0
    evidence_quote: str                        # verbatim text from comms/logs
    evidence_pod: str                          # pod whose endpoint produced this text
    evidence_timestamp: datetime | None = None


# ---------------------------------------------------------------------------
# ExtendedMetrics — replaces GraphMetrics, aggregates all three layers
# ---------------------------------------------------------------------------


class ExtendedMetrics(BaseModel):
    """All computed metrics for the colony map, across three analysis layers."""

    # ── Layer A: structural (networkx) ───────────────────────────────────
    in_degree: dict[str, int] = Field(default_factory=dict)
    out_degree: dict[str, int] = Field(default_factory=dict)
    betweenness_centrality: dict[str, float] = Field(default_factory=dict)
    in_degree_centrality: dict[str, float] = Field(default_factory=dict)
    articulation_points: list[str] = Field(default_factory=list)
    strongly_connected_components: list[list[str]] = Field(default_factory=list)
    longest_path: list[str] = Field(default_factory=list)

    blast_radius: dict[str, BlastRadiusEntry] = Field(default_factory=dict)
    cascade_simulations: list[CascadeSimulation] = Field(default_factory=list)

    # ── Layer B: operational (metadata + logs) ───────────────────────────
    metadata_signals: list[MetadataSignal] = Field(default_factory=list)
    historical_edges: list[HistoricalEdge] = Field(default_factory=list)
    risk_scores: dict[str, RiskScore] = Field(default_factory=dict)

    # ── Layer C: LLM enrichment ──────────────────────────────────────────
    llm_derived_signals: list[LLMDerivedSignal] = Field(default_factory=list)

    # ── Convenience rankings (populated last) ────────────────────────────
    highest_blast_radius: list[str] = Field(default_factory=list)   # desc by blast_radius_count
    most_vulnerable: list[str] = Field(default_factory=list)         # desc by vulnerability_score
    highest_overall_risk: list[str] = Field(default_factory=list)    # desc by overall_risk


# ---------------------------------------------------------------------------
# Pod node
# ---------------------------------------------------------------------------


class PodNode(BaseModel):
    """Everything known about one habitat pod after a complete crawl."""
    id: str
    hostname: str
    port: int

    info: PodInfo | None = None
    status: PodStatus | None = None

    raw_dependencies: list[ResourceLink] = Field(default_factory=list)
    raw_supplies: list[ResourceLink] = Field(default_factory=list)

    logs: list[LogEntry] = Field(default_factory=list)
    comms: list[CommEntry] | None = None   # None = /comms returned 404

    crawl_errors: dict[str, str] = Field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.info.name if self.info else self.id

    @property
    def has_comms(self) -> bool:
        return self.comms is not None


# ---------------------------------------------------------------------------
# Reconciliation audit record
# ---------------------------------------------------------------------------


class ReconciliationIssue(BaseModel):
    edge_source: str
    edge_target: str
    resource: str
    state: EdgeState
    description: str


# ---------------------------------------------------------------------------
# Root map object written to /rover/output/map.json
# ---------------------------------------------------------------------------


class ColonyMap(BaseModel):
    """Complete colony map — self-contained for the reporting phase."""
    crawled_at: datetime
    gateway_url: str

    pods: dict[str, PodNode] = Field(default_factory=dict)
    edges: list[DependencyEdge] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)

    reconciliation_issues: list[ReconciliationIssue] = Field(default_factory=list)
    metrics: ExtendedMetrics = Field(default_factory=ExtendedMetrics)

    discovery_order: list[str] = Field(default_factory=list)
    unreachable_pods: list[str] = Field(default_factory=list)

    def pod_count(self) -> int:
        return len(self.pods)

    def reconciled_edge_count(self) -> int:
        return sum(1 for e in self.edges if e.reconciled)

    def unreconciled_edge_count(self) -> int:
        return sum(1 for e in self.edges if not e.reconciled)
