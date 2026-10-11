"""Data model: one Inventory joins what Kubernetes knows with what vCenter knows."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

UNKNOWN_HOST = "<unmapped>"


@dataclass
class EsxiHost:
    name: str
    cluster: str = ""
    connection_state: str = "connected"
    in_maintenance: bool = False


@dataclass
class VirtualMachine:
    name: str
    esxi_host: str
    power_state: str = "poweredOn"
    guest_hostname: str = ""
    ips: list[str] = field(default_factory=list)
    cluster: str = ""


@dataclass
class DrsRule:
    name: str
    cluster: str
    kind: str            # "anti-affinity" | "affinity" | "other"
    enabled: bool
    mandatory: bool
    vms: list[str] = field(default_factory=list)


@dataclass
class Node:
    name: str
    ips: list[str] = field(default_factory=list)
    control_plane: bool = False
    ready: bool = True
    schedulable: bool = True
    alloc_cpu_m: int = 0
    alloc_mem_mi: int = 0
    vm: Optional[str] = None
    esxi_host: str = UNKNOWN_HOST


@dataclass
class EtcdMember:
    endpoint: str        # e.g. https://10.10.1.12:2379
    address: str         # host part of the endpoint
    stacked_on_node: Optional[str] = None
    vm: Optional[str] = None
    esxi_host: str = UNKNOWN_HOST


@dataclass
class Workload:
    kind: str            # Deployment | StatefulSet
    namespace: str
    name: str
    desired: int
    pod_nodes: list[str] = field(default_factory=list)   # node of each *ready* pod
    min_available: Optional[int] = None                    # resolved from a matching PDB
    pdb: Optional[str] = None
    req_cpu_m: int = 0                                      # per-pod requests
    req_mem_mi: int = 0

    @property
    def key(self) -> str:
        return f"{self.kind}/{self.namespace}/{self.name}"


@dataclass
class Inventory:
    cluster_name: str = "kubernetes"
    hosts: list[EsxiHost] = field(default_factory=list)
    vms: list[VirtualMachine] = field(default_factory=list)
    drs_rules: list[DrsRule] = field(default_factory=list)
    nodes: list[Node] = field(default_factory=list)
    etcd: list[EtcdMember] = field(default_factory=list)
    etcd_mode: str = "unknown"          # external | stacked | unknown
    vip_holder: Optional[str] = None    # node holding the kube-vip control-plane lease
    workloads: list[Workload] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
