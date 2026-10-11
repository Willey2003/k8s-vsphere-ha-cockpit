"""The core: what happens to *this* cluster if one (or two) ESXi hosts die right now?

Everything here is a pure function of an Inventory, so it is easy to test and
never touches vCenter or the cluster.

Impact model (the first minutes after a host loss, before anything reschedules):
  * etcd       - quorum survives only if  alive_members >= n//2 + 1
  * API        - needs at least one control-plane node AND etcd quorum
  * VIP        - kube-vip fails over if its holder dies and another manager survives
  * workloads  - ready pods on the dead host are gone; 0 left = outage,
                 fewer than a PDB's minAvailable = PDB violation
  * capacity   - can the surviving schedulable nodes absorb the dead host's pod
                 requests once Kubernetes evicts them (~5 min default toleration)?
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from itertools import combinations

from .model import Inventory, UNKNOWN_HOST

SAFE, DEGRADED, APP_OUTAGE, CLUSTER_DOWN = 0, 1, 2, 3
VERDICT_NAMES = {SAFE: "SAFE", DEGRADED: "DEGRADED", APP_OUTAGE: "APP OUTAGE", CLUSTER_DOWN: "CLUSTER DOWN"}


@dataclass
class WorkloadImpact:
    workload: str
    ready_before: int
    ready_after: int
    min_available: int | None
    outage: bool
    pdb_violated: bool


@dataclass
class HostFailureResult:
    failed_hosts: list[str]
    verdict: int = SAFE
    etcd_total: int = 0
    etcd_alive: int = 0
    etcd_needed: int = 0
    etcd_quorum: bool = True
    apiservers_alive: int = 0
    api_available: bool = True
    vip_failover: bool = False
    nodes_lost: list[str] = field(default_factory=list)
    workloads_down: list[WorkloadImpact] = field(default_factory=list)
    pdb_violations: list[WorkloadImpact] = field(default_factory=list)
    cpu_needed_m: int = 0
    cpu_free_m: int = 0
    mem_needed_mi: int = 0
    mem_free_mi: int = 0
    capacity_ok: bool = True
    reasons: list[str] = field(default_factory=list)

    @property
    def verdict_name(self) -> str:
        return VERDICT_NAMES[self.verdict]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["verdict_name"] = self.verdict_name
        return d


def candidate_hosts(inv: Inventory) -> list[str]:
    """Hosts worth simulating: every host that runs at least one cluster VM (plus all known hosts)."""
    used = {n.esxi_host for n in inv.nodes} | {m.esxi_host for m in inv.etcd}
    used.discard(UNKNOWN_HOST)
    return sorted(used | {h.name for h in inv.hosts})


def simulate(inv: Inventory, failed: list[str] | tuple[str, ...]) -> HostFailureResult:
    dead = set(failed)
    r = HostFailureResult(failed_hosts=sorted(dead))

    # --- etcd quorum -------------------------------------------------------
    r.etcd_total = len(inv.etcd)
    r.etcd_needed = r.etcd_total // 2 + 1 if r.etcd_total else 0
    r.etcd_alive = sum(1 for m in inv.etcd if m.esxi_host not in dead)
    r.etcd_quorum = r.etcd_total == 0 or r.etcd_alive >= r.etcd_needed
    if not r.etcd_quorum:
        r.reasons.append(
            f"etcd loses quorum: {r.etcd_alive}/{r.etcd_total} members left, {r.etcd_needed} needed "
            f"(members on {', '.join(sorted(dead))}: "
            f"{', '.join(m.vm or m.address for m in inv.etcd if m.esxi_host in dead)})")

    # --- control plane / API ----------------------------------------------
    node_host = {n.name: n.esxi_host for n in inv.nodes}
    r.nodes_lost = sorted(n.name for n in inv.nodes if n.esxi_host in dead)
    cps = [n for n in inv.nodes if n.control_plane]
    r.apiservers_alive = sum(1 for n in cps if n.esxi_host not in dead and n.ready)
    r.api_available = r.etcd_quorum and (r.apiservers_alive > 0 or not cps)
    if cps and r.apiservers_alive == 0:
        r.reasons.append("no kube-apiserver survives (all control-plane nodes on the failed host(s))")
    if inv.vip_holder and node_host.get(inv.vip_holder) in dead and r.apiservers_alive > 0:
        r.vip_failover = True
        r.reasons.append(f"API VIP moves away from {inv.vip_holder} (kube-vip leader election, ~seconds)")

    # --- workloads ---------------------------------------------------------
    for w in inv.workloads:
        before = len(w.pod_nodes)
        after = sum(1 for n in w.pod_nodes if node_host.get(n, UNKNOWN_HOST) not in dead)
        if before == after:
            continue
        outage = w.desired > 0 and after == 0
        pdb_bad = w.min_available is not None and after < w.min_available
        imp = WorkloadImpact(w.key, before, after, w.min_available, outage, pdb_bad)
        if outage:
            r.workloads_down.append(imp)
        elif pdb_bad:
            r.pdb_violations.append(imp)
    if r.workloads_down:
        r.reasons.append(f"{len(r.workloads_down)} workload(s) lose every ready replica: "
                         + ", ".join(i.workload for i in r.workloads_down[:5])
                         + (" ..." if len(r.workloads_down) > 5 else ""))
    if r.pdb_violations:
        r.reasons.append(f"{len(r.pdb_violations)} PodDisruptionBudget(s) violated")

    # --- capacity to reschedule -------------------------------------------
    req: dict[str, list[int]] = {}
    for w in inv.workloads:
        for n in w.pod_nodes:
            c = req.setdefault(n, [0, 0])
            c[0] += w.req_cpu_m
            c[1] += w.req_mem_mi
    for n in inv.nodes:
        used = req.get(n.name, [0, 0])
        if n.esxi_host in dead:
            r.cpu_needed_m += used[0]
            r.mem_needed_mi += used[1]
        elif n.schedulable and n.ready and not n.control_plane:
            r.cpu_free_m += max(0, n.alloc_cpu_m - used[0])
            r.mem_free_mi += max(0, n.alloc_mem_mi - used[1])
    r.capacity_ok = r.cpu_needed_m <= r.cpu_free_m and r.mem_needed_mi <= r.mem_free_mi
    if not r.capacity_ok:
        r.reasons.append(
            f"survivors cannot absorb evicted pods (need {r.cpu_needed_m}m CPU / {r.mem_needed_mi}Mi, "
            f"free {r.cpu_free_m}m / {r.mem_free_mi}Mi)")

    # --- verdict -----------------------------------------------------------
    if not r.api_available:
        r.verdict = CLUSTER_DOWN
    elif r.workloads_down:
        r.verdict = APP_OUTAGE
    elif r.pdb_violations or not r.capacity_ok or r.etcd_alive < r.etcd_total:
        r.verdict = DEGRADED
    if r.verdict == SAFE and not r.nodes_lost and r.etcd_alive == r.etcd_total:
        r.reasons.append("no cluster VMs on this host")
    elif r.verdict == DEGRADED and not r.reasons:
        r.reasons.append(f"etcd keeps quorum with {r.etcd_alive}/{r.etcd_total}; no spare member left")
    return r


@dataclass
class Report:
    cluster: str
    hosts: list[str]
    single: list[HostFailureResult]
    double: list[HostFailureResult]
    placement: dict
    drs: list[dict]
    warnings: list[str]

    @property
    def spof_hosts(self) -> list[str]:
        return [r.failed_hosts[0] for r in self.single if r.verdict == CLUSTER_DOWN]

    @property
    def worst(self) -> int:
        return max((r.verdict for r in self.single), default=SAFE)

    def to_dict(self) -> dict:
        return {
            "cluster": self.cluster,
            "hosts": self.hosts,
            "worst_verdict": VERDICT_NAMES[self.worst],
            "single_points_of_failure": self.spof_hosts,
            "single_host_failures": [r.to_dict() for r in self.single],
            "double_host_failures": [r.to_dict() for r in self.double],
            "placement": self.placement,
            "drs_recommendations": self.drs,
            "warnings": self.warnings,
        }


def placement_map(inv: Inventory) -> dict:
    out: dict[str, dict] = {}
    for h in candidate_hosts(inv) + ([UNKNOWN_HOST] if any(
            n.esxi_host == UNKNOWN_HOST for n in inv.nodes) or any(
            m.esxi_host == UNKNOWN_HOST for m in inv.etcd) else []):
        out[h] = {"control_plane": [], "workers": [], "etcd": []}
    for n in inv.nodes:
        out.setdefault(n.esxi_host, {"control_plane": [], "workers": [], "etcd": []})
        out[n.esxi_host]["control_plane" if n.control_plane else "workers"].append(n.name)
    for m in inv.etcd:
        out.setdefault(m.esxi_host, {"control_plane": [], "workers": [], "etcd": []})
        out[m.esxi_host]["etcd"].append(m.vm or m.address)
    return out


def run(inv: Inventory, depth: int = 1, drs: list[dict] | None = None) -> Report:
    hosts = candidate_hosts(inv)
    single = [simulate(inv, [h]) for h in hosts]
    double = [simulate(inv, list(c)) for c in combinations(hosts, 2)] if depth >= 2 else []
    return Report(inv.cluster_name, hosts, single, double, placement_map(inv), drs or [], list(inv.warnings))
