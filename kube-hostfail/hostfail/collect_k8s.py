"""Read-only Kubernetes inventory.

`fetch_raw()` talks to the API (get/list only) and returns plain JSON-able dicts.
`build()` turns that raw snapshot into Inventory objects - a pure function, which is
also how snapshot files (`kube-hostfail snapshot`) and the test fixtures are loaded.
"""
from __future__ import annotations

import math
import re
from urllib.parse import urlparse

from .model import Node, EtcdMember, Workload

KUBE_VIP_LEASES = ("plndr-cp-lock",)   # kube-vip control-plane leader lease
CP_LABELS = ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")


# ---------------------------------------------------------------- quantities
def cpu_m(q) -> int:
    if q in (None, ""):
        return 0
    s = str(q)
    if s.endswith("m"):
        return int(float(s[:-1]))
    if s.endswith("n"):
        return int(float(s[:-1]) / 1e6)
    return int(float(s) * 1000)


_MEM = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 ** 2,
        "k": 1000 / 1024 ** 2, "K": 1000 / 1024 ** 2, "M": 1e6 / 1024 ** 2, "G": 1e9 / 1024 ** 2, "T": 1e12 / 1024 ** 2}


def mem_mi(q) -> int:
    if q in (None, ""):
        return 0
    s = str(q)
    m = re.fullmatch(r"([0-9.]+)([A-Za-z]*)", s)
    if not m:
        return 0
    num, unit = float(m.group(1)), m.group(2)
    if not unit:
        return int(num / 1024 ** 2)
    return int(num * _MEM.get(unit, 1))


# ---------------------------------------------------------------- API access
def fetch_raw(kubeconfig: str | None = None, context: str | None = None) -> dict:
    from kubernetes import client, config
    try:
        if kubeconfig or context:
            config.load_kube_config(config_file=kubeconfig, context=context)
        else:
            config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config(config_file=kubeconfig, context=context)

    core, apps, policy, coord = (client.CoreV1Api(), client.AppsV1Api(),
                                 client.PolicyV1Api(), client.CoordinationV1Api())
    ser = client.ApiClient().sanitize_for_serialization

    def items(resp):
        return ser(resp)["items"]

    raw = {
        "nodes": items(core.list_node()),
        "pods": items(core.list_pod_for_all_namespaces()),
        "replicasets": items(apps.list_replica_set_for_all_namespaces()),
        "deployments": items(apps.list_deployment_for_all_namespaces()),
        "statefulsets": items(apps.list_stateful_set_for_all_namespaces()),
        "pdbs": items(policy.list_pod_disruption_budget_for_all_namespaces()),
        "leases": [],
    }
    for name in KUBE_VIP_LEASES:
        try:
            raw["leases"].append(ser(coord.read_namespaced_lease(name, "kube-system")))
        except client.ApiException:
            pass
    return raw


# ---------------------------------------------------------------- pure build
def _labels_match(selector: dict | None, labels: dict) -> bool:
    if not selector:
        return False
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for e in selector.get("matchExpressions") or []:
        k, op, vals = e.get("key"), e.get("operator"), e.get("values") or []
        if op == "In" and labels.get(k) not in vals:
            return False
        if op == "NotIn" and labels.get(k) in vals:
            return False
        if op == "Exists" and k not in labels:
            return False
        if op == "DoesNotExist" and k in labels:
            return False
    return True


def _pdb_min(pdb_spec: dict, desired: int) -> int | None:
    def val(x, round_up):
        if isinstance(x, str) and x.endswith("%"):
            f = float(x[:-1]) / 100 * desired
            return math.ceil(f) if round_up else math.floor(f)
        return int(x)
    if pdb_spec.get("minAvailable") is not None:
        return val(pdb_spec["minAvailable"], True)
    if pdb_spec.get("maxUnavailable") is not None:
        return max(0, desired - val(pdb_spec["maxUnavailable"], True))
    return None


def _pod_ready(pod: dict) -> bool:
    st = pod.get("status") or {}
    if st.get("phase") != "Running":
        return False
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in st.get("conditions") or [])


def _requests(spec: dict) -> tuple[int, int]:
    cpu = mem = 0
    for c in spec.get("containers") or []:
        r = (c.get("resources") or {}).get("requests") or {}
        cpu += cpu_m(r.get("cpu"))
        mem += mem_mi(r.get("memory"))
    return cpu, mem


def build(raw: dict):
    """Return (nodes, etcd_members, etcd_mode, vip_holder, workloads)."""
    nodes = []
    for n in raw.get("nodes", []):
        md, st, sp = n["metadata"], n.get("status") or {}, n.get("spec") or {}
        labels = md.get("labels") or {}
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in st.get("conditions") or [])
        alloc = st.get("allocatable") or {}
        nodes.append(Node(
            name=md["name"],
            ips=[a["address"] for a in st.get("addresses") or [] if a.get("type") in ("InternalIP", "ExternalIP")],
            control_plane=any(l in labels for l in CP_LABELS),
            ready=ready, schedulable=not sp.get("unschedulable", False),
            alloc_cpu_m=cpu_m(alloc.get("cpu")), alloc_mem_mi=mem_mi(alloc.get("memory"))))

    pods = raw.get("pods", [])

    # etcd: stacked (etcd static pods) or external (--etcd-servers on kube-apiserver)
    etcd, mode = [], "unknown"
    stacked = [p for p in pods if p["metadata"].get("namespace") == "kube-system"
               and (p["metadata"].get("labels") or {}).get("component") == "etcd"]
    if stacked:
        mode = "stacked"
        for p in stacked:
            node = (p.get("spec") or {}).get("nodeName")
            ip = (p.get("status") or {}).get("hostIP") or node
            etcd.append(EtcdMember(endpoint=f"https://{ip}:2379", address=ip, stacked_on_node=node))
    else:
        for p in pods:
            if (p["metadata"].get("labels") or {}).get("component") != "kube-apiserver":
                continue
            for c in (p.get("spec") or {}).get("containers") or []:
                for arg in (c.get("command") or []) + (c.get("args") or []):
                    if arg.startswith("--etcd-servers="):
                        for ep in arg.split("=", 1)[1].split(","):
                            host = urlparse(ep).hostname or ep
                            if host not in {m.address for m in etcd}:
                                etcd.append(EtcdMember(endpoint=ep, address=host))
            if etcd:
                local = {"127.0.0.1", "localhost", "::1"}
                mode = "stacked" if all(m.address in local for m in etcd) else "external"
                if mode == "stacked":   # loopback endpoints: one member per control-plane node
                    etcd = [EtcdMember(endpoint=f"https://{n.ips[0] if n.ips else n.name}:2379",
                                       address=n.ips[0] if n.ips else n.name, stacked_on_node=n.name)
                            for n in nodes if n.control_plane]
                break

    vip = None
    for lease in raw.get("leases", []):
        holder = (lease.get("spec") or {}).get("holderIdentity")
        if holder:
            vip = holder
            break

    # workloads: Deployments (via ReplicaSets) and StatefulSets
    rs_owner = {}
    for rs in raw.get("replicasets", []):
        md = rs["metadata"]
        for o in md.get("ownerReferences") or []:
            if o.get("kind") == "Deployment":
                rs_owner[(md["namespace"], md["name"])] = o["name"]
    pod_nodes: dict[tuple, list[str]] = {}
    for p in pods:
        md = p["metadata"]
        if not _pod_ready(p):
            continue
        for o in md.get("ownerReferences") or []:
            key = None
            if o.get("kind") == "ReplicaSet" and (md["namespace"], o["name"]) in rs_owner:
                key = ("Deployment", md["namespace"], rs_owner[(md["namespace"], o["name"])])
            elif o.get("kind") == "StatefulSet":
                key = ("StatefulSet", md["namespace"], o["name"])
            if key:
                pod_nodes.setdefault(key, []).append((p.get("spec") or {}).get("nodeName", ""))

    pdbs = raw.get("pdbs", [])
    workloads = []
    for kind, objs in (("Deployment", raw.get("deployments", [])), ("StatefulSet", raw.get("statefulsets", []))):
        for o in objs:
            md, sp = o["metadata"], o.get("spec") or {}
            desired = sp.get("replicas", 1) or 0
            tmpl = sp.get("template") or {}
            tlabels = (tmpl.get("metadata") or {}).get("labels") or {}
            cpu, mem = _requests(tmpl.get("spec") or {})
            w = Workload(kind=kind, namespace=md["namespace"], name=md["name"], desired=desired,
                         pod_nodes=pod_nodes.get((kind, md["namespace"], md["name"]), []),
                         req_cpu_m=cpu, req_mem_mi=mem)
            for pdb in pdbs:
                if pdb["metadata"]["namespace"] == md["namespace"] and \
                        _labels_match((pdb.get("spec") or {}).get("selector"), tlabels):
                    w.min_available = _pdb_min(pdb.get("spec") or {}, desired)
                    w.pdb = pdb["metadata"]["name"]
                    break
            workloads.append(w)
    return nodes, etcd, mode, vip, workloads
