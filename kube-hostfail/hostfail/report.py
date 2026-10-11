"""Human-readable output: terminal text and Markdown."""
from __future__ import annotations

from .model import Inventory
from .simulate import Report, CLUSTER_DOWN, APP_OUTAGE, DEGRADED

_ICON = {0: "OK  ", 1: "WARN", 2: "FAIL", 3: "DOWN"}


def text(inv: Inventory, rep: Report) -> str:
    L = []
    L.append(f"kube-hostfail  cluster={rep.cluster}  etcd={inv.etcd_mode} ({len(inv.etcd)} members)  "
             f"nodes={len(inv.nodes)}  esxi_hosts={len(rep.hosts)}")
    L.append("")
    L.append("PLACEMENT (where Kubernetes really lives)")
    for host, p in rep.placement.items():
        parts = []
        if p["etcd"]:
            parts.append("etcd: " + ", ".join(p["etcd"]))
        if p["control_plane"]:
            parts.append("control-plane: " + ", ".join(p["control_plane"]))
        if p["workers"]:
            parts.append("workers: " + ", ".join(p["workers"]))
        L.append(f"  {host:<34} " + (" | ".join(parts) or "-"))
    if inv.vip_holder:
        L.append(f"  API VIP held by: {inv.vip_holder}")
    L.append("")
    L.append("IF THIS ESXi HOST FAILED RIGHT NOW")
    for r in rep.single:
        L.append(f"  [{_ICON[r.verdict]}] {r.failed_hosts[0]:<30} {r.verdict_name:<12} "
                 f"etcd {r.etcd_alive}/{r.etcd_total}  apiservers {r.apiservers_alive}")
        for why in r.reasons:
            L.append(f"         - {why}")
    if rep.double:
        bad = [r for r in rep.double if r.verdict >= APP_OUTAGE]
        L.append("")
        L.append(f"TWO-HOST FAILURES: {len(rep.double) - len(bad)}/{len(rep.double)} combinations keep apps up")
        for r in bad[:10]:
            L.append(f"  [{_ICON[r.verdict]}] {' + '.join(r.failed_hosts):<40} {r.verdict_name}")
    L.append("")
    L.append("DRS ANTI-AFFINITY")
    if not rep.drs:
        L.append("  (fewer than 2 mapped etcd / control-plane VMs - nothing to separate)")
    for d in rep.drs:
        L.append(f"  {d['group']:<14} {d['status'].upper():<9} {', '.join(d['vms'])}")
        if d.get("govc"):
            L.append(f"    fix (review first): {d['govc']}")
        if d.get("note"):
            L.append(f"    note: {d['note']}")
    if rep.warnings:
        L.append("")
        L.append("WARNINGS")
        for w in rep.warnings:
            L.append(f"  - {w}")
    L.append("")
    if rep.spof_hosts:
        L.append(f"RESULT: NOT highly available - losing {', '.join(rep.spof_hosts)} takes the cluster down.")
    elif rep.worst >= APP_OUTAGE:
        L.append("RESULT: control plane survives any single host, but some apps would go down.")
    elif rep.worst == DEGRADED:
        L.append("RESULT: survives any single host failure, with reduced redundancy.")
    else:
        L.append("RESULT: survives any single ESXi host failure.")
    return "\n".join(L)


def markdown(inv: Inventory, rep: Report) -> str:
    L = [f"# kube-hostfail report: `{rep.cluster}`", ""]
    L += ["| ESXi host | etcd | control plane | workers |", "|---|---|---|---|"]
    for host, p in rep.placement.items():
        L.append(f"| {host} | {', '.join(p['etcd']) or '-'} | {', '.join(p['control_plane']) or '-'} | "
                 f"{', '.join(p['workers']) or '-'} |")
    L += ["", "| If this host fails | Verdict | etcd alive | API | Why |", "|---|---|---|---|---|"]
    for r in rep.single:
        L.append(f"| {r.failed_hosts[0]} | **{r.verdict_name}** | {r.etcd_alive}/{r.etcd_total} | "
                 f"{'up' if r.api_available else 'down'} | {'; '.join(r.reasons) or '-'} |")
    if rep.drs:
        L += ["", "## DRS anti-affinity", ""]
        for d in rep.drs:
            L.append(f"- **{d['group']}**: {d['status']} ({', '.join(d['vms'])})")
            if d.get("govc"):
                L.append(f"  - `{d['govc']}`")
    return "\n".join(L) + "\n"
