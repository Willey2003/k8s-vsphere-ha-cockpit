"""Join Kubernetes objects to vCenter VMs, and VMs to ESXi hosts.

Matching order for a node (first hit wins):
  1. explicit override  (HOSTFAIL_VM_MAP="node-or-ip=vm-name,...")
  2. VM name == node name (case-insensitive, short names compared too)
  3. VM guest hostname == node name
  4. any node InternalIP appears in the VM's guest IPs
For an etcd member the endpoint address is matched against VM guest IPs, VM names
and guest hostnames, then against Kubernetes node IPs (stacked etcd).
"""
from __future__ import annotations

from .model import Inventory, VirtualMachine, UNKNOWN_HOST


def _short(name: str) -> str:
    return name.lower().split(".")[0]


def parse_overrides(spec: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


class VmIndex:
    def __init__(self, vms: list[VirtualMachine]):
        self.by_name = {v.name.lower(): v for v in vms}
        self.by_short: dict[str, VirtualMachine] = {}
        self.by_guest: dict[str, VirtualMachine] = {}
        self.by_ip: dict[str, VirtualMachine] = {}
        for v in vms:
            self.by_short.setdefault(_short(v.name), v)
            if v.guest_hostname:
                self.by_guest.setdefault(v.guest_hostname.lower(), v)
                self.by_guest.setdefault(_short(v.guest_hostname), v)
            for ip in v.ips:
                self.by_ip.setdefault(ip, v)

    def lookup_name(self, name: str) -> VirtualMachine | None:
        n = name.lower()
        return (self.by_name.get(n) or self.by_short.get(_short(n))
                or self.by_guest.get(n) or self.by_guest.get(_short(n)))


def resolve(inv: Inventory, overrides: dict[str, str] | None = None) -> Inventory:
    overrides = overrides or {}
    idx = VmIndex(inv.vms)
    node_by_ip = {ip: n for n in inv.nodes for ip in n.ips}

    for node in inv.nodes:
        vm = None
        hint = overrides.get(node.name.lower())
        if hint:
            vm = idx.lookup_name(hint)
        vm = vm or idx.lookup_name(node.name)
        if vm is None:
            for ip in node.ips:
                if ip in overrides:
                    vm = idx.lookup_name(overrides[ip])
                vm = vm or idx.by_ip.get(ip)
                if vm:
                    break
        if vm:
            node.vm, node.esxi_host = vm.name, vm.esxi_host
        else:
            node.vm, node.esxi_host = None, UNKNOWN_HOST
            inv.warnings.append(f"node {node.name}: no matching VM in vCenter")

    for m in inv.etcd:
        vm = None
        hint = overrides.get(m.address.lower())
        if hint:
            vm = idx.lookup_name(hint)
        vm = vm or idx.by_ip.get(m.address) or idx.lookup_name(m.address)
        if vm is None:
            node = node_by_ip.get(m.address)
            if node is None and m.stacked_on_node:
                node = next((n for n in inv.nodes if n.name == m.stacked_on_node), None)
            if node is not None:
                m.stacked_on_node = node.name
                m.vm, m.esxi_host = node.vm, node.esxi_host
                continue
        if vm:
            m.vm, m.esxi_host = vm.name, vm.esxi_host
        else:
            m.vm, m.esxi_host = None, UNKNOWN_HOST
            inv.warnings.append(
                f"etcd member {m.endpoint}: no VM with that IP/name (VMware Tools reporting IPs? "
                f"or set HOSTFAIL_VM_MAP={m.address}=<vm-name>)")
    return inv
