#!/usr/bin/env bash
# Prometheus + Grafana (kube-prometheus-stack) in $NS_OBS, tuned for kubeadm clusters.
# Why this differs from the default install - the things that make it "never work" on kubeadm:
#   * kube-controller-manager / scheduler / kube-proxy listen on 127.0.0.1 -> their targets stay red forever.
#     We turn those scrapes OFF instead of editing your control plane (nothing on the masters is touched).
#   * external etcd is not a pod -> opt-in scrape with its client cert (ETCD_METRICS=true).
#   * no default StorageClass -> PVCs stay Pending -> we fall back to emptyDir.
#   * Grafana/Prometheus need to live under a sub-path so one gateway port serves everything.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
for t in kubectl helm; do need $t; done

say "namespace $NS_OBS (privileged only because node-exporter needs hostNetwork/hostPath)"
kubectl create ns "$NS_OBS" --dry-run=client -o yaml | kubectl apply -f -
kubectl label ns "$NS_OBS" pod-security.kubernetes.io/enforce=privileged --overwrite

if ! kubectl -n "$NS_OBS" get secret cockpit-grafana-admin >/dev/null 2>&1; then
  say "generating Grafana admin password (stored only in the cluster)"
  kubectl -n "$NS_OBS" create secret generic cockpit-grafana-admin \
    --from-literal=admin-user=admin --from-literal=admin-password="$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 20)"
fi

args=(-f "$ROOT/deploy/observability/kps-values.yaml")
sc=${STORAGE_CLASS}
[ -z "$sc" ] && sc=$(kubectl get sc -o jsonpath='{range .items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")]}{.metadata.name}{end}' 2>/dev/null || true)
if [ -z "$sc" ] || [ "$sc" = none ]; then
  warn "no StorageClass -> Prometheus and Grafana use emptyDir (data is lost when the pod is recreated)"
else
  say "using StorageClass $sc (Prometheus $PROM_STORAGE)"
  args+=(--set "prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.storageClassName=$sc"
         --set "prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.accessModes[0]=ReadWriteOnce"
         --set "prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.resources.requests.storage=$PROM_STORAGE"
         --set "grafana.persistence.enabled=true" --set "grafana.persistence.storageClassName=$sc" --set "grafana.persistence.size=2Gi")
fi
[ "$GRAFANA_ANONYMOUS" = true ] && args+=(--set "grafana.grafana\.ini.auth\.anonymous.enabled=true" --set "grafana.grafana\.ini.auth\.anonymous.org_role=Viewer")
[ "$NODE_EXPORTER" = true ] || args+=(--set "nodeExporter.enabled=false" --set "prometheus-node-exporter.enabled=false")

if [ "$ETCD_METRICS" = true ]; then
  [ -n "$ETCD_ENDPOINTS" ] || die "ETCD_METRICS=true needs ETCD_ENDPOINTS=ip1,ip2,ip3"
  kubectl -n "$NS_OBS" get secret etcd-client >/dev/null 2>&1 || die "create the etcd client secret first: scripts/etcd-client-secret.sh (see README)"
  say "enabling external etcd scrape: $ETCD_ENDPOINTS"
  i=0; for ep in ${ETCD_ENDPOINTS//,/ }; do args+=(--set "kubeEtcd.endpoints[$i]=$ep"); i=$((i+1)); done
  args+=(-f "$ROOT/deploy/observability/kps-values-etcd.yaml")
fi

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null
say "installing kube-prometheus-stack $KPS_VERSION"
helm upgrade --install kps prometheus-community/kube-prometheus-stack --version "$KPS_VERSION" \
  --namespace "$NS_OBS" "${args[@]}" --wait --timeout "$HELM_TIMEOUT"

say "dashboards and alert rules for kube-hostfail"
kubectl -n "$NS_OBS" apply -f "$ROOT/deploy/observability/hostfail-rules.yaml" -f "$ROOT/deploy/observability/hostfail-dashboard.yaml"
say "done. Grafana admin password:  kubectl -n $NS_OBS get secret cockpit-grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d"
