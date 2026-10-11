"""Builders for raw snapshots shaped like a kubeadm-on-vSphere lab:
3 external etcd VMs, 3 managers behind kube-vip, 4 workers, 3 ESXi hosts.
All names/IPs are example values.
"""
from __future__ import annotations

import copy

HOSTS = ["esx001.corp.example", "esx002.corp.example", "esx003.corp.example"]

ETCD = [("etcd-1", "10.10.1.12"), ("etcd-2", "10.10.1.13"), ("etcd-3", "10.10.1.19")]
MANAGERS = [("k8s-manager-1", "10.10.1.14"), ("k8s-manager-2", "10.10.1.21"), ("k8s-manager-3", "10.10.1.22")]
WORKERS = [("k8s-worker-1", "10.10.1.15"), ("k8s-worker-2", "10.10.1.16"),
           ("k8s-worker-3", "10.10.1.17"), ("k8s-worker-4", "10.10.1.18")]

# healthy, spread placement
SPREAD = {
    "etcd-1": 0, "etcd-2": 1, "etcd-3": 2,
    "k8s-manager-1": 0, "k8s-manager-2": 1, "k8s-manager-3": 2,
    "k8s-worker-1": 0, "k8s-worker-2": 1, "k8s-worker-3": 2, "k8s-worker-4": 0,
}


def _node(name, ip, cp):
    labels = {"kubernetes.io/hostname": name}
    if cp:
        labels["node-role.kubernetes.io/control-plane"] = ""
    return {"metadata": {"name": name, "labels": labels},
            "spec": {"unschedulable": False, **({"taints": [{"key": "node-role.kubernetes.io/control-plane",
                                                              "effect": "NoSchedule"}]} if cp else {})},
            "status": {"addresses": [{"type": "InternalIP", "address": ip}, {"type": "Hostname", "address": name}],
                       "allocatable": {"cpu": "4", "memory": "8Gi"},
                       "conditions": [{"type": "Ready", "status": "True"}]}}


def _pod(ns, name, node, owner_kind, owner, labels, ready=True, extra=None):
    p = {"metadata": {"namespace": ns, "name": name, "labels": labels,
                      "ownerReferences": [{"kind": owner_kind, "name": owner}] if owner else []},
         "spec": {"nodeName": node, "containers": [{"name": "c"}]},
         "status": {"phase": "Running", "hostIP": "",
                    "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}
    if extra:
        p["spec"].update(extra)
    return p


def _deploy(ns, name, replicas, labels, cpu="250m", mem="256Mi"):
    return {"metadata": {"namespace": ns, "name": name},
            "spec": {"replicas": replicas, "selector": {"matchLabels": labels},
                     "template": {"metadata": {"labels": labels},
                                  "spec": {"containers": [{"name": "c", "resources": {"requests": {"cpu": cpu, "memory": mem}}}]}}}}


def raw_snapshot(placement: dict | None = None, *, workloads: list[tuple] | None = None,
                 drs_rules: list[dict] | None = None, vip_holder: str = "k8s-manager-1",
                 guest_ips: bool = True) -> dict:
    placement = {**SPREAD, **(placement or {})}
    vms = []
    for name, ip in ETCD + MANAGERS + WORKERS:
        vms.append({"name": name.upper() if name.startswith("k8s") else name,
                    "esxi_host": HOSTS[placement[name]], "power_state": "poweredOn",
                    "guest_hostname": f"{name}.corp.example", "ips": [ip] if guest_ips else [],
                    "cluster": "LAB-CL01"})
    nodes = [_node(n, ip, True) for n, ip in MANAGERS] + [_node(n, ip, False) for n, ip in WORKERS]
    etcd_arg = "--etcd-servers=" + ",".join(f"https://{ip}:2379" for _, ip in ETCD)
    pods = [_pod("kube-system", f"kube-apiserver-{n}", n, "Node", n, {"component": "kube-apiserver", "tier": "control-plane"},
                 extra={"containers": [{"name": "kube-apiserver", "command": ["kube-apiserver", "--secure-port=6443", etcd_arg]}]})
            for n, _ in MANAGERS]

    # default workloads: (ns, name, [nodes of ready pods], pdb_min_available or None)
    workloads = workloads if workloads is not None else [
        ("shop", "frontend", ["k8s-worker-1", "k8s-worker-2"], 1),
        ("shop", "catalog", ["k8s-worker-1", "k8s-worker-4"], 1),   # both replicas end up on esx001
        ("shop", "pricing", ["k8s-worker-3"], None),                 # single replica
    ]
    deployments, replicasets, pdbs = [], [], []
    for ns, name, on_nodes, pdb_min in workloads:
        labels = {"app": name}
        deployments.append(_deploy(ns, name, len(on_nodes), labels))
        rs = f"{name}-7d9f"
        replicasets.append({"metadata": {"namespace": ns, "name": rs, "ownerReferences": [{"kind": "Deployment", "name": name}]}})
        for i, n in enumerate(on_nodes):
            pods.append(_pod(ns, f"{rs}-{i}", n, "ReplicaSet", rs, labels))
        if pdb_min is not None:
            pdbs.append({"metadata": {"namespace": ns, "name": f"{name}-pdb"},
                         "spec": {"minAvailable": pdb_min, "selector": {"matchLabels": labels}}})

    return {
        "cluster_name": "lab",
        "k8s": {"nodes": nodes, "pods": pods, "replicasets": replicasets, "deployments": deployments,
                "statefulsets": [], "pdbs": pdbs,
                "leases": [{"metadata": {"name": "plndr-cp-lock", "namespace": "kube-system"},
                            "spec": {"holderIdentity": vip_holder}}] if vip_holder else []},
        "vsphere": {"hosts": [{"name": h, "cluster": "LAB-CL01", "connection_state": "connected",
                               "in_maintenance": False} for h in HOSTS],
                    "vms": vms, "drs_rules": copy.deepcopy(drs_rules or [])},
    }


def drifted() -> dict:
    """What DRS can quietly do: etcd-1 and etcd-2 end up on the same host as manager-2."""
    return raw_snapshot({"etcd-1": 1, "etcd-2": 1, "k8s-manager-1": 1})
