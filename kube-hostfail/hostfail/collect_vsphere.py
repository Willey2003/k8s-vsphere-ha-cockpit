"""Read-only vCenter inventory: hosts, VMs (with their current ESXi host) and DRS rules.

Required vCenter privileges: the built-in *Read-only* role is enough (System.View/Read).
No task is ever created; only the PropertyCollector is used.
"""
from __future__ import annotations

import ssl
from urllib.parse import urlparse

from .model import EsxiHost, VirtualMachine, DrsRule


def _connect(server: str, user: str, password: str, insecure: bool):
    from pyVim.connect import SmartConnect
    u = urlparse(server if "://" in server else f"https://{server}")
    kw = dict(host=u.hostname, port=u.port or 443, user=user, pwd=password)
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kw["sslContext"] = ctx
    return SmartConnect(**kw)


def _collect(si, obj_type, props: list[str]) -> list[tuple[object, dict]]:
    from pyVmomi import vmodl
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(content.rootFolder, [obj_type], True)
    try:
        spec = vmodl.query.PropertyCollector.FilterSpec(
            objectSet=[vmodl.query.PropertyCollector.ObjectSpec(
                obj=view, skip=True,
                selectSet=[vmodl.query.PropertyCollector.TraversalSpec(
                    name="traverseEntities", path="view", skip=False, type=type(view))])],
            propSet=[vmodl.query.PropertyCollector.PropertySpec(type=obj_type, pathSet=props, all=False)])
        out = []
        result = content.propertyCollector.RetrievePropertiesEx([spec], vmodl.query.PropertyCollector.RetrieveOptions())
        while result:
            for o in result.objects:
                out.append((o.obj, {p.name: p.val for p in (o.propSet or [])}))
            if not result.token:
                break
            result = content.propertyCollector.ContinueRetrievePropertiesEx(result.token)
        return out
    finally:
        view.Destroy()


def collect(server: str, user: str, password: str, insecure: bool = True):
    """Return (hosts, vms, drs_rules)."""
    from pyVmomi import vim
    from pyVim.connect import Disconnect

    si = _connect(server, user, password, insecure)
    try:
        cluster_name: dict[str, str] = {}
        rules: list[DrsRule] = []
        vm_name_by_moid: dict[str, str] = {}

        for obj, p in _collect(si, vim.VirtualMachine, ["name"]):
            vm_name_by_moid[obj._moId] = p.get("name", obj._moId)

        for obj, p in _collect(si, vim.ClusterComputeResource, ["name", "host", "configurationEx"]):
            cname = p.get("name", obj._moId)
            for h in p.get("host", []) or []:
                cluster_name[h._moId] = cname
            cfg = p.get("configurationEx")
            for r in getattr(cfg, "rule", None) or []:
                if isinstance(r, vim.cluster.AntiAffinityRuleSpec):
                    kind = "anti-affinity"
                elif isinstance(r, vim.cluster.AffinityRuleSpec):
                    kind = "affinity"
                else:
                    kind = "other"
                rules.append(DrsRule(
                    name=r.name, cluster=cname, kind=kind, enabled=bool(r.enabled),
                    mandatory=bool(getattr(r, "mandatory", False)),
                    vms=[vm_name_by_moid.get(v._moId, v._moId) for v in (getattr(r, "vm", None) or [])]))

        hosts, host_name = [], {}
        for obj, p in _collect(si, vim.HostSystem,
                               ["name", "runtime.connectionState", "runtime.inMaintenanceMode"]):
            name = p.get("name", obj._moId)
            host_name[obj._moId] = name
            hosts.append(EsxiHost(
                name=name, cluster=cluster_name.get(obj._moId, ""),
                connection_state=str(p.get("runtime.connectionState", "connected")),
                in_maintenance=bool(p.get("runtime.inMaintenanceMode", False))))

        vms = []
        for obj, p in _collect(si, vim.VirtualMachine,
                               ["name", "runtime.host", "runtime.powerState", "guest.hostName",
                                "guest.ipAddress", "guest.net", "config.template"]):
            if p.get("config.template"):
                continue
            ips: list[str] = []
            if p.get("guest.ipAddress"):
                ips.append(p["guest.ipAddress"])
            for nic in p.get("guest.net", []) or []:
                for ip in getattr(nic, "ipAddress", None) or []:
                    if ip not in ips:
                        ips.append(ip)
            h = p.get("runtime.host")
            hname = host_name.get(h._moId, h._moId) if h is not None else ""
            vms.append(VirtualMachine(
                name=p.get("name", obj._moId), esxi_host=hname,
                power_state=str(p.get("runtime.powerState", "")),
                guest_hostname=p.get("guest.hostName") or "", ips=ips,
                cluster=cluster_name.get(h._moId, "") if h is not None else ""))
        return hosts, vms, rules
    finally:
        Disconnect(si)
