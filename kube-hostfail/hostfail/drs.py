"""Check whether DRS is told to keep the HA-critical VMs apart, and print the fix.

kube-hostfail never creates rules itself - it prints the govc / PowerCLI command
for a human to review and run.
"""
from __future__ import annotations

from itertools import combinations

from .model import Inventory


def _groups(inv: Inventory) -> dict[str, list[str]]:
    etcd_vms = sorted({m.vm for m in inv.etcd if m.vm})
    cp_vms = sorted({n.vm for n in inv.nodes if n.control_plane and n.vm})
    groups = {}
    if len(etcd_vms) >= 2:
        groups["etcd"] = etcd_vms
    # with stacked etcd the two groups are the same VMs - one rule covers both
    if len(cp_vms) >= 2 and set(cp_vms) != set(etcd_vms):
        groups["control-plane"] = cp_vms
    return groups


def recommendations(inv: Inventory) -> list[dict]:
    vm_cluster = {v.name: v.cluster for v in inv.vms}
    hosts_per_cluster: dict[str, int] = {}
    for h in inv.hosts:
        if h.connection_state == "connected" and not h.in_maintenance:
            hosts_per_cluster[h.cluster] = hosts_per_cluster.get(h.cluster, 0) + 1

    out = []
    for group, vms in _groups(inv).items():
        clusters = sorted({vm_cluster.get(v, "") for v in vms})
        cluster = clusters[0] if len(clusters) == 1 else ""
        pairs = set(combinations(vms, 2))
        covered, disabled_cover = set(), set()
        rules = []
        for rule in inv.drs_rules:
            if rule.kind != "anti-affinity":
                continue
            rp = {tuple(sorted(p)) for p in combinations(rule.vms, 2)}
            hit = pairs & rp
            if hit:
                rules.append(rule.name)
                (covered if rule.enabled else disabled_cover).update(hit)
        missing = pairs - covered
        if not missing:
            status = "ok"
        elif disabled_cover & missing and not (missing - disabled_cover):
            status = "disabled"
        elif covered:
            status = "partial"
        else:
            status = "missing"

        name = f"k8s-{inv.cluster_name}-{group}-anti-affinity".replace("_", "-").lower()
        rec = {
            "group": group,
            "vms": vms,
            "cluster": cluster or ",".join(c for c in clusters if c),
            "status": status,
            "existing_rules": rules,
            "uncovered_pairs": sorted(" + ".join(p) for p in missing),
        }
        if len(clusters) > 1:
            rec["note"] = ("VMs are in different vSphere clusters - DRS rules are per cluster; "
                           "separation already depends on cluster placement.")
        elif status != "ok":
            vm_args = " ".join(vms)
            rec["govc"] = f"govc cluster.rule.create -cluster '{cluster}' -name {name} -enable -anti-affinity {vm_args}"
            ps_vms = ",".join(f"'{v}'" for v in vms)
            rec["powercli"] = (f"New-DrsRule -Cluster (Get-Cluster '{cluster}') -Name '{name}' "
                               f"-KeepTogether $false -Enabled $true -VM (Get-VM {ps_vms})")
            n_hosts = hosts_per_cluster.get(cluster, 0)
            if n_hosts and n_hosts < len(vms):
                rec["note"] = (f"only {n_hosts} usable host(s) for {len(vms)} VMs - DRS cannot fully separate "
                               f"them; the rule still minimises co-location.")
            else:
                rec["note"] = ("Also enable 'vSphere HA should respect VM anti-affinity rules during failover' "
                               "in the cluster's HA settings.")
        out.append(rec)
    return out
