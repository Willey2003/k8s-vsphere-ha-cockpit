#!/usr/bin/env bash
# Istio (sidecar mode) + Jaeger + Kiali + the Gateway that fronts everything.
# Nothing existing is changed: sidecars are injected only into namespaces labelled istio-injection=enabled
# (this project labels only $NS_DEMO). Istio adds CRDs and a few ClusterRoles - listed in the README.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
for t in kubectl helm python3; do need $t; done
route_vars

if ! kubectl get crd gateways.gateway.networking.k8s.io >/dev/null 2>&1; then
  say "installing Gateway API CRDs $GATEWAY_API_VERSION (standard channel)"
  kubectl apply -f "https://github.com/kubernetes-sigs/gateway-api/releases/download/${GATEWAY_API_VERSION}/standard-install.yaml"
else
  say "Gateway API CRDs already present - leaving them alone"
fi

helm repo add istio https://istio-release.storage.googleapis.com/charts >/dev/null 2>&1 || true
helm repo add kiali https://kiali.org/helm-charts >/dev/null 2>&1 || true
helm repo update istio kiali >/dev/null

kubectl create ns "$NS_ISTIO" --dry-run=client -o yaml | kubectl apply -f -
say "istio base + istiod $ISTIO_VERSION"
helm upgrade --install istio-base istio/base --version "$ISTIO_VERSION" -n "$NS_ISTIO" --set defaultRevision=default --wait --timeout "$HELM_TIMEOUT"
helm upgrade --install istiod istio/istiod --version "$ISTIO_VERSION" -n "$NS_ISTIO" \
  -f "$ROOT/deploy/observability/istiod-values.yaml" --wait --timeout "$HELM_TIMEOUT"

say "Jaeger (in-memory/badger, lab grade) + trace sampling"
kubectl apply -f "$ROOT/deploy/observability/jaeger.yaml" -f "$ROOT/deploy/observability/telemetry.yaml"
kubectl -n "$NS_ISTIO" rollout status deploy/jaeger --timeout=300s

say "Prometheus scraping for the mesh"
if kubectl get crd servicemonitors.monitoring.coreos.com >/dev/null 2>&1; then
  render < "$ROOT/deploy/observability/istio-monitors.yaml" | kubectl apply -f -
else
  warn "Prometheus Operator CRDs not found - run scripts/10-observability.sh first, then re-run this script"
fi

say "Kiali $KIALI_VERSION"
helm upgrade --install kiali kiali/kiali-server --version "$KIALI_VERSION" -n "$NS_ISTIO" \
  -f "$ROOT/deploy/observability/kiali-values.yaml" --wait --timeout "$HELM_TIMEOUT"

kubectl create ns "$NS_SYS" --dry-run=client -o yaml | kubectl apply -f -
if [ "$GATEWAY_MODE" = own ]; then
  say "Gateway $GATEWAY_NAMESPACE/$GATEWAY_NAME (served by Istio)"
  render < "$ROOT/deploy/gateway.yaml" | kubectl apply -f -
  kubectl -n "$GATEWAY_NAMESPACE" wait --for=condition=Programmed gateway/"$GATEWAY_NAME" --timeout=180s || warn "Gateway not Programmed yet - see: kubectl -n $GATEWAY_NAMESPACE describe gateway $GATEWAY_NAME"
  kubectl -n "$GATEWAY_NAMESPACE" get svc "${GATEWAY_NAME}-istio" -o wide || true
else
  say "GATEWAY_MODE=existing: routes will attach to $GATEWAY_NAMESPACE/$GATEWAY_NAME"
fi
say "mesh ready. Next: scripts/30-apps.sh"
