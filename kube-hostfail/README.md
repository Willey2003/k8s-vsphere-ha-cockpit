# kube-hostfail

**Would your Kubernetes cluster survive the loss of one ESXi host — right now?**

Kubernetes sees *nodes*. vSphere sees *VMs on hosts*. DRS, a maintenance-mode evacuation or a
well-meaning admin can put two of your three etcd VMs on the same ESXi host, and neither side
will tell you. Your "HA" cluster is then one host failure away from losing etcd quorum.

kube-hostfail joins the two views (read-only) and simulates every host failure:

| Checks | How |
|---|---|
| **etcd quorum** | external etcd from `--etcd-servers` on kube-apiserver, or stacked etcd pods; members matched to VMs by guest IP |
| **API availability** | control-plane nodes left + etcd quorum; kube-vip VIP holder failover |
| **Workloads** | Deployments/StatefulSets losing every ready replica; PodDisruptionBudget violations |
| **Capacity** | can surviving workers absorb the evicted pods' requests? |
| **DRS** | are etcd / control-plane VMs covered by an enabled VM-VM anti-affinity rule? Prints the `govc`/PowerCLI fix — never applies it |

Outputs: terminal report, Markdown, JSON, a web view, and Prometheus metrics with alerts.

## Try it without vSphere

```bash
pip install .
kube-hostfail report --snapshot examples/lab-drifted.json --depth 2
```

## Run against your environment

```bash
export VSPHERE_SERVER=vcenter.corp.example VSPHERE_USER='svc-hostfail@vsphere.local' VSPHERE_PASSWORD=...
kube-hostfail report --kubeconfig ~/.kube/readonly-config --cluster-name lab
kube-hostfail check ...          # exit code 2 if any single host is a single point of failure
kube-hostfail snapshot -o s.json # save once, replay offline
kube-hostfail serve --port 9877  # web view + /metrics
```

Permissions: vCenter **Read-only** role; Kubernetes `get/list` on nodes, pods, replicasets,
deployments, statefulsets, poddisruptionbudgets and one lease (a ready-made read-only ClusterRole is in `../deploy/hostfail.yaml`).

If VMware Tools don't report IPs for some VMs, map them explicitly:
`HOSTFAIL_VM_MAP="10.10.1.12=etcd-1,k8s-w1=K8S-WORKER-01"`.

## Model and limits

The verdict describes the first minutes after a host dies, before vSphere HA restarts VMs or
Kubernetes reschedules pods: `SAFE` · `DEGRADED` (redundancy lost, PDB violated or not enough spare
capacity) · `APP OUTAGE` · `CLUSTER DOWN` (etcd quorum or every apiserver lost).
Not modelled yet: PV/datastore placement, network partitions, DaemonSets.

## Tests

`pytest` runs unit tests on fixtures and, if `vcsim` + `govc` are installed, an end-to-end test
against govmomi's vCenter simulator: it builds the VMs, vMotions two etcd members together,
checks the SPOF is detected, applies the suggested DRS rule and checks it is recognised.
