#!/usr/bin/env bash
# Remove everything this project installed. Leaves CRDs (Gateway API, Prometheus Operator, Istio) in place
# unless you pass --crds, because other things may use them.
set -uo pipefail
. "$(dirname "$0")/lib.sh"
need kubectl; need helm
purge_crds=false; [ "${1:-}" = --crds ] && purge_crds=true
read -r -p "Delete namespaces $NS_DEMO $NS_SYS $NS_OBS and the Istio/Kiali/Jaeger releases in $NS_ISTIO? [y/N] " a
[ "$a" = y ] || exit 0

kubectl delete httproute -A -l app.kubernetes.io/part-of=ha-cockpit --ignore-not-found
kubectl delete ns "$NS_DEMO" --ignore-not-found
helm -n "$NS_ISTIO" uninstall kiali 2>/dev/null
kubectl delete -f "$ROOT/deploy/observability/jaeger.yaml" -f "$ROOT/deploy/observability/telemetry.yaml" --ignore-not-found
helm -n "$NS_ISTIO" uninstall istiod 2>/dev/null
helm -n "$NS_ISTIO" uninstall istio-base 2>/dev/null
kubectl delete ns "$NS_ISTIO" --ignore-not-found
kubectl delete gateway -n "$GATEWAY_NAMESPACE" "$GATEWAY_NAME" --ignore-not-found
kubectl delete clusterrole,clusterrolebinding kube-hostfail-read cockpit-launcher-read --ignore-not-found
kubectl -n kube-system delete rolebinding,role kube-hostfail-vip-lease --ignore-not-found
kubectl delete ns "$NS_SYS" --ignore-not-found
helm -n "$NS_OBS" uninstall kps 2>/dev/null
kubectl delete ns "$NS_OBS" --ignore-not-found
if $purge_crds; then
  warn "removing CRDs (this deletes every object of these kinds cluster-wide)"
  kubectl get crd -o name | grep -E 'monitoring.coreos.com|istio.io' | xargs -r kubectl delete
fi
say "uninstalled"
