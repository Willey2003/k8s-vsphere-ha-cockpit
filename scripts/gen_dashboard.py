#!/usr/bin/env python3
"""Generate the Grafana dashboard ConfigMap for kube-hostfail (picked up by Grafana's sidecar).
Run:  python3 scripts/gen_dashboard.py > deploy/observability/hostfail-dashboard.yaml
"""
import json

DS = {"type": "prometheus", "uid": "${datasource}"}
_id = [0]


def nid():
    _id[0] += 1
    return _id[0]


def stat(title, expr, x, y, w=6, h=4, thresholds=None, mappings=None, unit="none", text="value"):
    return {"id": nid(), "type": "stat", "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [{"expr": expr, "refId": "A", "instant": True}],
            "options": {"colorMode": "background", "graphMode": "none", "textMode": text, "reduceOptions": {"calcs": ["lastNotNull"]}},
            "fieldConfig": {"defaults": {"unit": unit, "mappings": mappings or [],
                                         "thresholds": {"mode": "absolute", "steps": thresholds or [{"color": "green", "value": None}]}},
                            "overrides": []}}


def table(title, expr, x, y, w, h, rename=None, hide=None):
    transforms = [{"id": "labelsToFields", "options": {}}] if False else []
    transforms.append({"id": "organize", "options": {"excludeByName": {**{k: True for k in (hide or [])},
                                                                         "Time": True, "__name__": True, "Value": True, "job": True,
                                                                         "instance": True, "endpoint": True, "namespace": True,
                                                                         "pod": True, "service": True, "container": True},
                                                      "renameByName": rename or {}}})
    return {"id": nid(), "type": "table", "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [{"expr": expr, "refId": "A", "instant": True, "format": "table"}],
            "transformations": transforms, "options": {"showHeader": True}, "fieldConfig": {"defaults": {}, "overrides": []}}


verdict_map = [{"type": "value", "options": {"0": {"text": "SAFE", "color": "green"}, "1": {"text": "DEGRADED", "color": "yellow"},
                                             "2": {"text": "APP OUTAGE", "color": "orange"}, "3": {"text": "CLUSTER DOWN", "color": "red"}}}]

panels = [
    stat("ESXi hosts that are a single point of failure", "hostfail_single_points_of_failure", 0, 0,
         thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
    stat("etcd members discovered", "hostfail_etcd_members", 6, 0),
    stat("Unmapped nodes / etcd members", "hostfail_unmapped_objects", 12, 0,
         thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}]),
    stat("Last scan", "hostfail_last_scan_success", 18, 0,
         thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
         mappings=[{"type": "value", "options": {"0": {"text": "FAILING"}, "1": {"text": "OK"}}}]),
    {"id": nid(), "type": "bargauge", "title": "If this ESXi host fails: verdict (0 safe … 3 cluster down)", "datasource": DS,
     "gridPos": {"x": 0, "y": 4, "w": 12, "h": 8},
     "targets": [{"expr": "hostfail_host_failure_verdict", "refId": "A", "instant": True, "legendFormat": "{{esxi_host}} · {{verdict}}"}],
     "options": {"orientation": "horizontal", "displayMode": "gradient", "reduceOptions": {"calcs": ["lastNotNull"]}},
     "fieldConfig": {"defaults": {"min": 0, "max": 3, "mappings": verdict_map,
                                  "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "yellow", "value": 1},
                                                                                {"color": "orange", "value": 2}, {"color": "red", "value": 3}]}},
                     "overrides": []}},
    {"id": nid(), "type": "bargauge", "title": "etcd members still alive if this host fails", "datasource": DS,
     "gridPos": {"x": 12, "y": 4, "w": 12, "h": 8},
     "targets": [{"expr": "hostfail_host_failure_etcd_members_alive", "refId": "A", "instant": True, "legendFormat": "{{esxi_host}}"}],
     "options": {"orientation": "horizontal", "displayMode": "basic", "reduceOptions": {"calcs": ["lastNotNull"]}},
     "fieldConfig": {"defaults": {"min": 0, "max": 3, "decimals": 0,
                                  "thresholds": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "yellow", "value": 2},
                                                                                {"color": "green", "value": 3}]}}, "overrides": []}},
    table("Where etcd lives", "hostfail_etcd_member_placement", 0, 12, 8, 8, rename={"member": "etcd member", "vm": "VM", "esxi_host": "ESXi host"}),
    table("Where nodes live", "hostfail_node_placement", 8, 12, 16, 8, rename={"node": "node", "vm": "VM", "esxi_host": "ESXi host", "role": "role"}),
    table("DRS anti-affinity coverage (1 = problem)", "hostfail_drs_antiaffinity_missing", 0, 20, 12, 6, rename={"group": "group", "status": "status"}),
    {"id": nid(), "type": "timeseries", "title": "Workloads losing every replica, per failed host", "datasource": DS,
     "gridPos": {"x": 12, "y": 20, "w": 12, "h": 6},
     "targets": [{"expr": "hostfail_host_failure_workloads_down", "refId": "A", "legendFormat": "{{esxi_host}}"}],
     "fieldConfig": {"defaults": {"decimals": 0}, "overrides": []}, "options": {}},
]

dash = {
    "uid": "kube-hostfail", "title": "kube-hostfail · ESXi host failure impact", "schemaVersion": 39, "version": 1,
    "tags": ["kube-hostfail", "vsphere", "ha"], "timezone": "browser", "refresh": "30s", "time": {"from": "now-6h", "to": "now"},
    "templating": {"list": [{"name": "datasource", "type": "datasource", "query": "prometheus", "label": "Data source", "current": {}}]},
    "panels": panels,
}

indented = "\n".join("    " + line for line in json.dumps(dash, indent=2).splitlines())
print(f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: kube-hostfail-dashboard
  labels:
    grafana_dashboard: "1"
    app.kubernetes.io/part-of: ha-cockpit
data:
  kube-hostfail.json: |
{indented}""")
