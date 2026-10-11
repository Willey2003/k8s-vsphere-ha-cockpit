"""End-to-end against govmomi's vCenter simulator (vcsim).

Builds a 3-host vSphere cluster with VMs named like a kubeadm lab, reads it with the real
pyvmomi collector, then vMotions two etcd VMs together and checks that kube-hostfail
flags the new single point of failure. Skipped if `vcsim`/`govc` are not on PATH.
"""
import os
import shutil
import socket
import subprocess
import time
from dataclasses import asdict

import pytest

from hostfail import collect_vsphere
from hostfail.scan import Config, scan
from hostfail.simulate import CLUSTER_DOWN

from fixtures import raw_snapshot, ETCD, MANAGERS, WORKERS

pytestmark = pytest.mark.skipif(not (shutil.which("vcsim") and shutil.which("govc")), reason="vcsim/govc not installed")

H = ["DC0_C0_H0", "DC0_C0_H1", "DC0_C0_H2"]
HOST_PATH = "/DC0/host/DC0_C0/{}"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def vc():
    port = _free_port()
    proc = subprocess.Popen(["vcsim", "-l", f"127.0.0.1:{port}", "-cluster", "1", "-host", "3", "-vm", "0",
                             "-standalone-host", "0", "-pool", "0"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    env = {**os.environ, "GOVC_URL": f"https://user:pass@127.0.0.1:{port}/sdk", "GOVC_INSECURE": "1"}
    for _ in range(50):
        if subprocess.run(["govc", "about"], env=env, capture_output=True).returncode == 0:
            break
        time.sleep(0.2)

    def govc(*args):
        r = subprocess.run(["govc", *args], env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout

    spread = [0, 1, 2]
    for i, (name, ip) in enumerate(ETCD):
        _vm(govc, name, ip, H[spread[i]])
    for i, (name, ip) in enumerate(MANAGERS):
        _vm(govc, name.upper(), ip, H[spread[i]])
    for i, (name, ip) in enumerate(WORKERS):
        _vm(govc, name.upper(), ip, H[[0, 1, 2, 0][i]])
    yield govc, f"127.0.0.1:{port}"
    proc.terminate()
    proc.wait(5)


def _vm(govc, name, ip, host):
    govc("vm.create", "-host", HOST_PATH.format(host), "-net", "VM Network", "-on=true", name)
    govc("vm.change", "-vm", name, "-e", f"SET.guest.ipAddress={ip}", "-e", f"SET.guest.hostName={name.lower()}.corp.example")


def _scan(server):
    hosts, vms, rules = collect_vsphere.collect(server, "user", "pass", insecure=True)
    raw = raw_snapshot()
    raw["vsphere"] = {"hosts": [asdict(h) for h in hosts], "vms": [asdict(v) for v in vms],
                      "drs_rules": [asdict(r) for r in rules]}
    return scan(Config(), raw=raw)


def test_live_collector_reads_hosts_vms_and_ips(vc):
    _, server = vc
    hosts, vms, _ = collect_vsphere.collect(server, "user", "pass")
    assert sorted(h.name for h in hosts) == H
    etcd1 = next(v for v in vms if v.name == "etcd-1")
    assert etcd1.esxi_host == H[0] and "10.10.1.12" in etcd1.ips and etcd1.cluster == "DC0_C0"


def test_spread_then_drift_then_drs_rule(vc):
    govc, server = vc
    inv, rep = _scan(server)
    assert not inv.warnings, inv.warnings
    assert rep.spof_hosts == []

    # DRS / an admin "helpfully" vMotions two etcd members onto the same host
    govc("vm.migrate", "-host", HOST_PATH.format(H[1]), "etcd-1")
    inv, rep = _scan(server)
    res = {r.failed_hosts[0]: r for r in rep.single}
    assert res[H[1]].verdict == CLUSTER_DOWN
    assert rep.spof_hosts == [H[1]]
    etcd = next(d for d in rep.drs if d["group"] == "etcd")
    assert etcd["status"] == "missing" and etcd["cluster"] == "DC0_C0"

    # apply the printed fix to the simulator and verify kube-hostfail sees it
    govc("cluster.rule.create", "-cluster", "DC0_C0", "-name", "k8s-lab-etcd-anti-affinity",
         "-enable", "-anti-affinity", "etcd-1", "etcd-2", "etcd-3")
    govc("vm.migrate", "-host", HOST_PATH.format(H[0]), "etcd-1")
    inv, rep = _scan(server)
    assert next(d for d in rep.drs if d["group"] == "etcd")["status"] == "ok"
    assert rep.spof_hosts == []
