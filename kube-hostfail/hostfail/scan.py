"""Glue: collect (live or from a snapshot file), join, simulate."""
from __future__ import annotations

import json
import os
from dataclasses import asdict

from . import collect_k8s, drs, mapping, simulate
from .model import Inventory, EsxiHost, VirtualMachine, DrsRule


class Config:
    def __init__(self, **kw):
        env = os.environ.get
        self.cluster_name = kw.get("cluster_name") or env("HOSTFAIL_CLUSTER_NAME", "kubernetes")
        self.kubeconfig = kw.get("kubeconfig") or env("KUBECONFIG")
        self.context = kw.get("context") or env("HOSTFAIL_K8S_CONTEXT")
        self.vsphere_server = kw.get("vsphere_server") or env("VSPHERE_SERVER")
        self.vsphere_user = kw.get("vsphere_user") or env("VSPHERE_USER")
        self.vsphere_password = kw.get("vsphere_password") or env("VSPHERE_PASSWORD")
        insecure = kw.get("vsphere_insecure")
        self.vsphere_insecure = insecure if insecure is not None else env("VSPHERE_INSECURE", "true").lower() == "true"
        self.snapshot = kw.get("snapshot") or env("HOSTFAIL_SNAPSHOT")
        self.vm_map = mapping.parse_overrides(kw.get("vm_map") or env("HOSTFAIL_VM_MAP"))
        self.depth = int(kw.get("depth") or env("HOSTFAIL_DEPTH", "1"))
        self.exclude_namespaces = set(filter(None, (kw.get("exclude_namespaces")
                                                    or env("HOSTFAIL_EXCLUDE_NAMESPACES", "")).split(",")))


def collect_raw(cfg: Config) -> dict:
    """Live read-only collection into the snapshot format."""
    if not cfg.vsphere_server:
        raise SystemExit("VSPHERE_SERVER is not set (or use --snapshot FILE)")
    hosts, vms, rules = __import__("hostfail.collect_vsphere", fromlist=["collect"]).collect(
        cfg.vsphere_server, cfg.vsphere_user or "", cfg.vsphere_password or "", cfg.vsphere_insecure)
    return {
        "cluster_name": cfg.cluster_name,
        "k8s": collect_k8s.fetch_raw(cfg.kubeconfig, cfg.context),
        "vsphere": {"hosts": [asdict(h) for h in hosts], "vms": [asdict(v) for v in vms],
                    "drs_rules": [asdict(r) for r in rules]},
    }


def load_snapshot(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def inventory_from_raw(raw: dict, cfg: Config) -> Inventory:
    vs = raw.get("vsphere", {})
    inv = Inventory(
        cluster_name=raw.get("cluster_name") or cfg.cluster_name,
        hosts=[EsxiHost(**h) for h in vs.get("hosts", [])],
        vms=[VirtualMachine(**v) for v in vs.get("vms", [])],
        drs_rules=[DrsRule(**r) for r in vs.get("drs_rules", [])],
    )
    nodes, etcd, mode, vip, workloads = collect_k8s.build(raw.get("k8s", {}))
    inv.nodes, inv.etcd, inv.etcd_mode, inv.vip_holder = nodes, etcd, mode, vip
    inv.workloads = [w for w in workloads if w.namespace not in cfg.exclude_namespaces]
    if not etcd:
        inv.warnings.append("could not discover etcd members (no etcd pods, no --etcd-servers flag visible)")
    mapping.resolve(inv, cfg.vm_map)
    return inv


def scan(cfg: Config, raw: dict | None = None):
    if raw is None:
        raw = load_snapshot(cfg.snapshot) if cfg.snapshot else collect_raw(cfg)
    inv = inventory_from_raw(raw, cfg)
    return inv, simulate.run(inv, depth=cfg.depth, drs=drs.recommendations(inv))
