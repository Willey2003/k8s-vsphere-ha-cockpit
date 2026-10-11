#!/usr/bin/env bash
# kube-hostfail + App Launcher + demo shop, and one HTTPRoute per app (the launcher builds its tiles from them).
set -euo pipefail
. "$(dirname "$0")/lib.sh"
for t in kubectl python3; do need $t; done
route_vars

kubectl create ns "$NS_SYS" --dry-run=client -o yaml | kubectl apply -f -

# ---------------------------------------------------------------- kube-hostfail
HOSTFAIL_ENV=''; HOSTFAIL_ENVFROM=''; HOSTFAIL_VOLUME_MOUNTS=''; HOSTFAIL_VOLUMES=''
if [ "$HOSTFAIL_MODE" = live ]; then
  [ -n "${VSPHERE_SERVER:-}" ] && [ -n "${VSPHERE_USER:-}" ] && [ -n "${VSPHERE_PASSWORD:-}" ] || die "HOSTFAIL_MODE=live needs VSPHERE_SERVER, VSPHERE_USER, VSPHERE_PASSWORD"
  say "kube-hostfail: LIVE mode (read-only vCenter account $VSPHERE_USER @ $VSPHERE_SERVER)"
  kubectl -n "$NS_SYS" create secret generic hostfail-vsphere \
    --from-literal=VSPHERE_SERVER="$VSPHERE_SERVER" --from-literal=VSPHERE_USER="$VSPHERE_USER" \
    --from-literal=VSPHERE_PASSWORD="$VSPHERE_PASSWORD" --from-literal=VSPHERE_INSECURE="${VSPHERE_INSECURE:-true}" \
    ${HOSTFAIL_VM_MAP:+--from-literal=HOSTFAIL_VM_MAP="$HOSTFAIL_VM_MAP"} \
    --dry-run=client -o yaml | kubectl apply -f -
  HOSTFAIL_ENVFROM=$'          envFrom:\n            - secretRef: {name: hostfail-vsphere}'
else
  say "kube-hostfail: DEMO mode (replays examples/lab-drifted.json - no vCenter needed)"
  kubectl -n "$NS_SYS" create configmap hostfail-snapshot \
    --from-file=snapshot.json="$ROOT/kube-hostfail/examples/lab-drifted.json" --dry-run=client -o yaml | kubectl apply -f -
  HOSTFAIL_ENV=$'            - {name: HOSTFAIL_SNAPSHOT, value: /snap/snapshot.json}'
  HOSTFAIL_VOLUME_MOUNTS=$'          volumeMounts: [{name: snap, mountPath: /snap, readOnly: true}]'
  HOSTFAIL_VOLUMES=$'      volumes:\n        - name: snap\n          configMap: {name: hostfail-snapshot}'
fi
export HOSTFAIL_ENV HOSTFAIL_ENVFROM HOSTFAIL_VOLUME_MOUNTS HOSTFAIL_VOLUMES
render < "$ROOT/deploy/hostfail.yaml" | kubectl apply -f -

# ---------------------------------------------------------------- launcher + demo
kubectl -n "$NS_SYS" create configmap launcher-code --from-file=app.py="$ROOT/launcher/app.py" --dry-run=client -o yaml | kubectl apply -f -
render < "$ROOT/deploy/launcher.yaml" | kubectl apply -f -
render < "$ROOT/deploy/demo-shop.yaml" | kubectl apply -f -
kubectl -n "$NS_DEMO" create configmap shop-code --from-file=shop.py="$ROOT/demo-shop/shop.py" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS_DEMO" rollout restart deploy >/dev/null 2>&1 || true   # pick up the code ConfigMap on first install

# ---------------------------------------------------------------- one HTTPRoute per app
route() {  # ns name path svc port title desc icon group [extra-annotation-lines]
  local ns=$1 name=$2 path=$3 svc=$4 port=$5 title=$6 desc=$7 icon=$8 group=$9 extra=${10:-}
  kubectl apply -f - <<YAML
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: $name
  namespace: $ns
  labels: {app.kubernetes.io/part-of: ha-cockpit}
  annotations:
    cockpit.io/title: "$title"
    cockpit.io/description: "$desc"
    cockpit.io/icon: "$icon"
    cockpit.io/group: "$group"
$extra
spec:
  parentRefs:
    - {name: $GATEWAY_NAME, namespace: $ROUTE_PARENT_NS}
$HOSTNAMES_BLOCK
  rules:
    - matches: [{path: {type: PathPrefix, value: $path}}]
      backendRefs: [{name: $svc, port: $port}]
YAML
}
say "publishing routes"
route "$NS_SYS"  launcher   /           launcher       80    "Launcher" "" "" "Cockpit" '    cockpit.io/hide: "true"'
route "$NS_SYS"  hostfail   /hostfail   kube-hostfail  80    "kube-hostfail" "Would the cluster survive an ESXi host failure?" "🛡" "Reliability" '    cockpit.io/health-path: /hostfail/healthz'
route "$NS_OBS"  grafana    /grafana    kps-grafana    80    "Grafana" "Dashboards" "📊" "Observability" '    cockpit.io/health-path: /grafana/api/health'
route "$NS_OBS"  prometheus /prometheus kps-prometheus 9090  "Prometheus" "Metrics, targets and alerts" "🔥" "Observability" '    cockpit.io/health-path: /prometheus/-/healthy'
route "$NS_ISTIO" kiali     /kiali      kiali          20001 "Kiali" "Service mesh graph" "🕸" "Service mesh" '    cockpit.io/health-path: /kiali/healthz'
route "$NS_ISTIO" jaeger    /jaeger     tracing        80    "Jaeger" "Distributed traces" "🔍" "Service mesh" '    cockpit.io/health-path: /jaeger/'
route "$NS_DEMO" shop       /shop       frontend       8080  "Demo shop" "3 services, 2 pricing versions, live load" "🛒" "Demo apps"

say "waiting for rollouts"
kubectl -n "$NS_SYS" rollout status deploy/kube-hostfail deploy/launcher --timeout=300s
kubectl -n "$NS_DEMO" rollout status deploy/frontend deploy/catalog deploy/pricing-v1 --timeout=300s || warn "demo pods not ready yet (sidecar injection needs istiod; check kubectl -n $NS_DEMO get pods)"
say "apps ready. Next: scripts/40-verify.sh"
