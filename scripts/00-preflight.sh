#!/usr/bin/env bash
# PREFLIGHT - strictly read-only. Changes nothing. Run this first; it tells you what the installer will do.
set -uo pipefail
. "$(dirname "$0")/lib.sh"
for t in kubectl helm python3; do need $t; done
fail=0
ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; fail=1; }
note() { printf '  \033[33mnote\033[0m %s\n' "$*"; }

say "cluster"
kubectl version -o json >/dev/null 2>&1 && ok "API reachable: $(kubectl config current-context)" || { bad "cannot reach the cluster with the current kubeconfig"; exit 1; }
kubectl auth can-i create clusterrole >/dev/null 2>&1 && ok "cluster-admin style rights (needed for CRDs / ClusterRoles)" || bad "your user cannot create ClusterRoles - run as a cluster admin"
printf '  nodes: '; kubectl get nodes --no-headers | awk '{printf "%s(%s) ",$1,$2}'; echo
kubectl get nodes --no-headers | awk '$2!="Ready"{f=1} END{exit f}' || bad "some nodes are not Ready"

say "what already exists (we never modify these)"
for ns in "$NS_OBS" "$NS_SYS" "$NS_DEMO" "$NS_ISTIO"; do
  if kubectl get ns "$ns" >/dev/null 2>&1; then note "namespace $ns already exists (re-run is fine; objects are applied idempotently)"; fi
done
kubectl get crd prometheuses.monitoring.coreos.com >/dev/null 2>&1 && note "Prometheus Operator CRDs already installed (another Prometheus stack exists). We reuse the CRDs; our Prometheus lives in $NS_OBS only." || ok "no existing Prometheus Operator CRDs"
kubectl get crd virtualservices.networking.istio.io >/dev/null 2>&1 && note "Istio CRDs already present - check 'kubectl get pods -A | grep istiod' before installing a second control plane" || ok "no existing Istio"
helm list -A 2>/dev/null | awk 'NR>1{print "      helm release: "$1" ("$2") "$9}' | head -20

say "storage (the usual reason Prometheus/Grafana pods stay Pending)"
sc=$(kubectl get sc -o jsonpath='{range .items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")]}{.metadata.name}{end}' 2>/dev/null)
if [ -n "$sc" ]; then ok "default StorageClass: $sc"; else note "no default StorageClass -> installer will use emptyDir (metrics lost on pod restart). Set STORAGE_CLASS=<name> to use a PVC."; fi

say "Gateway API / ingress"
if kubectl get crd gateways.gateway.networking.k8s.io >/dev/null 2>&1; then
  v=$(kubectl get crd gateways.gateway.networking.k8s.io -o jsonpath='{.metadata.annotations.gateway\.networking\.k8s\.io/bundle-version}')
  ok "Gateway API CRDs present (${v:-unknown version}); installer will not touch them"
else
  note "Gateway API CRDs missing -> 20-mesh.sh installs the standard channel $GATEWAY_API_VERSION"
fi
kubectl get gatewayclass 2>/dev/null | sed 's/^/      /'
kubectl get ns metallb-system >/dev/null 2>&1 && ok "MetalLB present (LoadBalancer IPs available)" || note "no MetalLB: set GATEWAY_SERVICE_TYPE=NodePort or use port-forward (scripts/port-forward.sh)"
[ "$GATEWAY_MODE" = existing ] && { kubectl -n "$GATEWAY_NAMESPACE" get gateway "$GATEWAY_NAME" >/dev/null 2>&1 && ok "existing Gateway $GATEWAY_NAMESPACE/$GATEWAY_NAME found" || bad "GATEWAY_MODE=existing but Gateway $GATEWAY_NAMESPACE/$GATEWAY_NAME not found"; }

say "capacity (requests this project adds, roughly: 2.5 CPU / 4 GiB with defaults)"
kubectl get nodes -o custom-columns=NODE:.metadata.name,CPU:.status.allocatable.cpu,MEM:.status.allocatable.memory --no-headers | sed 's/^/      /'

say "image for kube-hostfail"
note "HOSTFAIL_IMAGE=$HOSTFAIL_IMAGE (build with kube-hostfail/Dockerfile if your nodes cannot pull from ghcr.io)"
[ "$HOSTFAIL_MODE" = live ] && { [ -n "${VSPHERE_SERVER:-}" ] && [ -n "${VSPHERE_USER:-}" ] && [ -n "${VSPHERE_PASSWORD:-}" ] && ok "vCenter credentials provided via environment" || bad "HOSTFAIL_MODE=live needs VSPHERE_SERVER, VSPHERE_USER, VSPHERE_PASSWORD"; }

echo
[ $fail = 0 ] && say "preflight passed - nothing was changed" || { warn "preflight found blocking problems"; exit 1; }
