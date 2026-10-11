import json

import pytest

from hostfail import collect_k8s, simulate
from hostfail.cli import main
from hostfail.scan import Config, scan
from hostfail.simulate import SAFE, DEGRADED, APP_OUTAGE, CLUSTER_DOWN

from fixtures import raw_snapshot, drifted, HOSTS

E1, E2, E3 = HOSTS


def run(raw, **kw):
    return scan(Config(**kw), raw=raw)


def by_host(rep):
    return {r.failed_hosts[0]: r for r in rep.single}


def test_mapping_external_etcd_by_ip_and_nodes_by_name():
    inv, _ = run(raw_snapshot())
    assert inv.etcd_mode == "external"
    assert [m.vm for m in inv.etcd] == ["etcd-1", "etcd-2", "etcd-3"]
    # VM names are upper-case in vCenter, nodes lower-case: still matched
    assert {n.name: n.vm for n in inv.nodes}["k8s-manager-1"] == "K8S-MANAGER-1"
    assert not inv.warnings


def test_spread_cluster_survives_every_single_host():
    inv, rep = run(raw_snapshot(workloads=[("shop", "web", ["k8s-worker-1", "k8s-worker-2"], 1)]))
    res = by_host(rep)
    assert all(r.etcd_quorum and r.api_available for r in res.values())
    assert rep.spof_hosts == []
    # losing one of three etcd members is survivable but leaves no spare -> DEGRADED
    assert res[E1].verdict == DEGRADED
    assert res[E1].vip_failover is True      # VIP holder manager-1 is on esx001


def test_drifted_etcd_is_detected_as_single_point_of_failure():
    inv, rep = run(drifted())
    res = by_host(rep)
    assert res[E2].verdict == CLUSTER_DOWN
    assert res[E2].etcd_alive == 1 and res[E2].etcd_needed == 2
    assert rep.spof_hosts == [E2]
    assert any("etcd loses quorum" in x for x in res[E2].reasons)


def test_workload_outage_and_pdb_violation():
    _, rep = run(raw_snapshot())
    res = by_host(rep)
    # catalog has both replicas on esx001 (worker-1 and worker-4)
    assert [w.workload for w in res[E1].workloads_down] == ["Deployment/shop/catalog"]
    assert res[E1].verdict == APP_OUTAGE
    # pricing is a single replica on worker-3 / esx003
    assert [w.workload for w in res[E3].workloads_down] == ["Deployment/shop/pricing"]
    # frontend has one replica on esx002, PDB minAvailable=1, one left -> fine
    assert not res[E2].pdb_violations


def test_pdb_violation_when_survivors_below_min_available():
    raw = raw_snapshot(workloads=[("db", "api", ["k8s-worker-1", "k8s-worker-4", "k8s-worker-2"], 2)])
    _, rep = run(raw)
    r = by_host(rep)[E1]   # worker-1 and worker-4 on esx001 -> 1 left, min 2
    assert r.verdict == DEGRADED
    assert r.pdb_violations[0].ready_after == 1 and r.pdb_violations[0].min_available == 2


def test_unmapped_node_is_reported_not_guessed():
    raw = raw_snapshot()
    raw["vsphere"]["vms"] = [v for v in raw["vsphere"]["vms"] if v["name"] != "K8S-WORKER-2"]
    inv, rep = run(raw)
    assert any("k8s-worker-2" in w for w in inv.warnings)
    assert "<unmapped>" in rep.placement


def test_vm_map_override_when_tools_report_no_ips():
    raw = raw_snapshot(guest_ips=False)
    for v in raw["vsphere"]["vms"]:
        v["guest_hostname"] = ""
    inv, _ = run(raw)
    assert all(m.vm is None for m in inv.etcd)
    inv, _ = run(raw, vm_map="10.10.1.12=etcd-1,10.10.1.13=etcd-2,10.10.1.19=etcd-3")
    assert [m.vm for m in inv.etcd] == ["etcd-1", "etcd-2", "etcd-3"]


def test_stacked_etcd_follows_control_plane_nodes():
    raw = raw_snapshot({"k8s-manager-2": 0})       # managers 1+2 on esx001
    for p in raw["k8s"]["pods"]:
        if p["metadata"]["labels"].get("component") == "kube-apiserver":
            p["spec"]["containers"][0]["command"] = ["kube-apiserver", "--etcd-servers=https://127.0.0.1:2379"]
    inv, rep = run(raw)
    assert inv.etcd_mode == "stacked"
    assert sorted(m.esxi_host for m in inv.etcd) == [E1, E1, E3]
    assert by_host(rep)[E1].verdict == CLUSTER_DOWN


def test_double_failure_depth():
    _, rep = run(raw_snapshot(), depth=2)
    assert len(rep.double) == 3
    assert all(r.verdict == CLUSTER_DOWN for r in rep.double)   # 3 etcd members can't lose 2


def test_drs_missing_then_ok():
    _, rep = run(raw_snapshot())
    etcd = next(d for d in rep.drs if d["group"] == "etcd")
    assert etcd["status"] == "missing" and "govc cluster.rule.create" in etcd["govc"]
    rule = {"name": "etcd-apart", "cluster": "LAB-CL01", "kind": "anti-affinity", "enabled": True,
            "mandatory": False, "vms": ["etcd-1", "etcd-2", "etcd-3"]}
    _, rep = run(raw_snapshot(drs_rules=[rule]))
    assert next(d for d in rep.drs if d["group"] == "etcd")["status"] == "ok"
    rule["enabled"] = False
    _, rep = run(raw_snapshot(drs_rules=[rule]))
    assert next(d for d in rep.drs if d["group"] == "etcd")["status"] == "disabled"


def test_capacity_check():
    raw = raw_snapshot()
    for n in raw["k8s"]["nodes"]:
        n["status"]["allocatable"] = {"cpu": "300m", "memory": "300Mi"}
    _, rep = run(raw)
    assert any(not r.capacity_ok for r in rep.single)


@pytest.mark.parametrize("q,exp", [("4", 4000), ("3900m", 3900), ("250m", 250), ("1500000n", 1)])
def test_cpu_parse(q, exp):
    assert collect_k8s.cpu_m(q) == exp


@pytest.mark.parametrize("q,exp", [("8Gi", 8192), ("256Mi", 256), ("16303584Ki", 15921), ("1G", 953)])
def test_mem_parse(q, exp):
    assert collect_k8s.mem_mi(q) == exp


def test_cli_check_exit_code(tmp_path, capsys):
    f = tmp_path / "s.json"
    f.write_text(json.dumps(drifted()))
    assert main(["check", "--snapshot", str(f)]) == 2
    f.write_text(json.dumps(raw_snapshot()))
    assert main(["check", "--snapshot", str(f)]) == 0
    assert main(["report", "--snapshot", str(f), "--format", "markdown"]) == 0
    assert "kube-hostfail report" in capsys.readouterr().out
