#!/usr/bin/env bash
# Shared settings and helpers. Source this; do not run it.
# Override anything by exporting it first, or put it in ./cockpit.env (same folder as the scripts' parent).

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[ -f "$ROOT/cockpit.env" ] && . "$ROOT/cockpit.env"

# ---- namespaces (everything this project creates lives in these four) -------------------
# NS_OBS and NS_ISTIO are referenced by name inside the Helm values and Jaeger/Kiali config, so keep their defaults.
# NS_SYS and NS_DEMO are safe to rename.
NS_OBS=${NS_OBS:-cockpit-observability}   # Prometheus, Grafana, node-exporter
NS_SYS=${NS_SYS:-cockpit-system}          # gateway, launcher, kube-hostfail
NS_DEMO=${NS_DEMO:-cockpit-demo}          # 3-service demo shop (sidecar-injected)
NS_ISTIO=${NS_ISTIO:-istio-system}        # istiod, Jaeger, Kiali

# ---- versions (pinned; verified against the chart repos on 2026-10-10) ------------------
KPS_VERSION=${KPS_VERSION:-92.3.0}        # kube-prometheus-stack
ISTIO_VERSION=${ISTIO_VERSION:-1.30.5}
KIALI_VERSION=${KIALI_VERSION:-2.33.0}
GATEWAY_API_VERSION=${GATEWAY_API_VERSION:-v1.3.0}
HELM_TIMEOUT=${HELM_TIMEOUT:-10m}

# ---- how apps are reached -----------------------------------------------------------------
# GATEWAY_MODE=own      : create a Gateway "cockpit" in $NS_SYS served by Istio (self-contained, default)
# GATEWAY_MODE=existing : attach the routes to a Gateway you already run, e.g. HAProxy Unified Gateway
GATEWAY_MODE=${GATEWAY_MODE:-own}
GATEWAY_NAME=${GATEWAY_NAME:-cockpit}
GATEWAY_NAMESPACE=${GATEWAY_NAMESPACE:-$NS_SYS}
GATEWAY_SERVICE_TYPE=${GATEWAY_SERVICE_TYPE:-LoadBalancer}   # NodePort if you have no MetalLB/cloud LB
GATEWAY_LB_IP=${GATEWAY_LB_IP:-}                             # optional fixed MetalLB address
# in-cluster URL the launcher uses to health-check tiles *through* the gateway
GATEWAY_INTERNAL_URL=${GATEWAY_INTERNAL_URL:-http://${GATEWAY_NAME}-istio.${GATEWAY_NAMESPACE}.svc.cluster.local}
# optional: serve under one hostname (needed when the existing Gateway's listener has a hostname)
COCKPIT_HOST=${COCKPIT_HOST:-}

# ---- kube-hostfail -------------------------------------------------------------------------
HOSTFAIL_IMAGE=${HOSTFAIL_IMAGE:-ghcr.io/willey2003/kube-hostfail:0.1.0}
HOSTFAIL_MODE=${HOSTFAIL_MODE:-demo}         # demo = replay examples/lab-drifted.json ; live = read vCenter
HOSTFAIL_CLUSTER_NAME=${HOSTFAIL_CLUSTER_NAME:-lab}
HOSTFAIL_DEPTH=${HOSTFAIL_DEPTH:-2}
# live mode: export VSPHERE_SERVER / VSPHERE_USER / VSPHERE_PASSWORD (read-only vCenter account)

# ---- optional features ---------------------------------------------------------------------
GRAFANA_ANONYMOUS=${GRAFANA_ANONYMOUS:-true}   # anonymous *Viewer* so a tile click lands on dashboards
NODE_EXPORTER=${NODE_EXPORTER:-true}
ETCD_METRICS=${ETCD_METRICS:-false}            # scrape external etcd (needs ETCD_ENDPOINTS + client cert, see README)
ETCD_ENDPOINTS=${ETCD_ENDPOINTS:-}             # comma separated IPs, e.g. 10.10.1.12,10.10.1.13,10.10.1.19
STORAGE_CLASS=${STORAGE_CLASS:-}               # empty = use cluster default; none => emptyDir (no PVCs at all)
PROM_STORAGE=${PROM_STORAGE:-10Gi}

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }

# Substitute ${VARS} (only the names in SUBST_VARS) in stdin -> stdout. Pure python3, so no gettext/envsubst needed.
SUBST_VARS='NS_OBS NS_SYS NS_DEMO NS_ISTIO GATEWAY_NAME GATEWAY_NAMESPACE GATEWAY_SERVICE_TYPE GATEWAY_INTERNAL_URL COCKPIT_HOST HOSTFAIL_IMAGE HOSTFAIL_CLUSTER_NAME HOSTFAIL_DEPTH LB_ANNOTATIONS HOSTNAMES_BLOCK ROUTE_PARENT_NS HOSTFAIL_ENV HOSTFAIL_ENVFROM HOSTFAIL_VOLUME_MOUNTS HOSTFAIL_VOLUMES'
render() { local _SUBST="${SUBST_VARS}"; export _SUBST
  python3 -c '
import os, re, sys
names = os.environ["_SUBST"].split()
text = sys.stdin.read()
for n in names:
    text = re.sub(r"\$\{" + n + r"\}|\$" + n + r"(?![A-Za-z0-9_])", lambda m, n=n: os.environ.get(n, ""), text)
sys.stdout.write(text)' < /dev/stdin
}
# parentRefs / hostnames shared by every route we create
route_vars() {
  ROUTE_PARENT_NS=$GATEWAY_NAMESPACE
  if [ -n "$COCKPIT_HOST" ]; then HOSTNAMES_BLOCK=$'  hostnames: ["'"$COCKPIT_HOST"'"]'; else HOSTNAMES_BLOCK=''; fi
  LB_ANNOTATIONS=''
  if [ -n "$GATEWAY_LB_IP" ]; then
    LB_ANNOTATIONS=$'  infrastructure:\n    annotations:\n      metallb.universe.tf/loadBalancerIPs: "'"$GATEWAY_LB_IP"'"'
  fi
  export ROUTE_PARENT_NS HOSTNAMES_BLOCK LB_ANNOTATIONS
}
export NS_OBS NS_SYS NS_DEMO NS_ISTIO GATEWAY_NAME GATEWAY_NAMESPACE GATEWAY_SERVICE_TYPE GATEWAY_INTERNAL_URL COCKPIT_HOST HOSTFAIL_IMAGE HOSTFAIL_CLUSTER_NAME HOSTFAIL_DEPTH
