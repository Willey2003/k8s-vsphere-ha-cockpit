"""kube-hostfail command line.

  kube-hostfail report   [--snapshot FILE] [--format text|markdown|json] [--depth 2]
  kube-hostfail snapshot -o FILE        # save a live read-only snapshot (share it, replay it)
  kube-hostfail serve    [--port 9877] [--interval 60]
  kube-hostfail check    # exit 2 if any single ESXi host is a single point of failure (for CI/cron)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import __version__, report
from .scan import Config, scan, collect_raw
from .simulate import CLUSTER_DOWN


def _common(p):
    p.add_argument("--snapshot", help="read inventory from a snapshot JSON instead of live APIs")
    p.add_argument("--kubeconfig")
    p.add_argument("--context")
    p.add_argument("--vsphere-server")
    p.add_argument("--vsphere-user")
    p.add_argument("--cluster-name")
    p.add_argument("--vm-map", help="overrides, e.g. 10.10.1.12=etcd-1,k8s-w1=K8S-WORKER-01")
    p.add_argument("--depth", type=int, choices=(1, 2), help="also simulate pairs of hosts failing (2)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kube-hostfail", description="Would your Kubernetes cluster survive an ESXi host failure?")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="print a one-off report")
    _common(r)
    r.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    s = sub.add_parser("snapshot", help="save a read-only snapshot to a file")
    _common(s)
    s.add_argument("-o", "--output", required=True)
    v = sub.add_parser("serve", help="exporter + web view")
    _common(v)
    v.add_argument("--port", type=int, default=9877)
    v.add_argument("--interval", type=int, default=60)
    c = sub.add_parser("check", help="exit 2 if any single host failure takes the cluster down")
    _common(c)
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config(snapshot=a.snapshot, kubeconfig=a.kubeconfig, context=a.context,
                 vsphere_server=a.vsphere_server, vsphere_user=a.vsphere_user,
                 cluster_name=a.cluster_name, vm_map=a.vm_map, depth=a.depth)

    if a.cmd == "snapshot":
        raw = collect_raw(cfg)
        with open(a.output, "w") as f:
            json.dump(raw, f, indent=1)
        print(f"wrote {a.output}: {len(raw['k8s']['nodes'])} nodes, {len(raw['vsphere']['vms'])} VMs")
        return 0
    if a.cmd == "serve":
        from .server import serve
        serve(cfg, port=a.port, interval=a.interval)
        return 0

    inv, rep = scan(cfg)
    if a.cmd == "check":
        print(report.text(inv, rep))
        return 2 if rep.worst >= CLUSTER_DOWN else 0
    if a.format == "json":
        print(json.dumps({"report": rep.to_dict(), "inventory": inv.to_dict()}, indent=2))
    elif a.format == "markdown":
        print(report.markdown(inv, rep))
    else:
        print(report.text(inv, rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
