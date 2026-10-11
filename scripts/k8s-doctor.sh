#!/usr/bin/env bash
# READ-ONLY diagnosis of "why doesn't Prometheus/Grafana work on this kubeadm cluster?"
# Only kubectl get/describe/logs-style reads. Changes nothing. Safe to run on an existing production-style cluster.
set -uo pipefail
command -v kubectl >/dev/null || { echo "kubectl missing"; exit 1; }
kubectl version -o json >/dev/null 2>&1 || { echo "cannot reach the cluster with the current kubeconfig"; exit 1; }

n_bad=0
finding() { n_bad=$((n_bad+1)); printf '\n\033[1;31m[%d] %s\033[0m\n    %s\n' "$n_bad" "$1" "$2"; }
okline()  { printf '\033[32mok\033[0m   %s\n' "$*"; }

echo "== monitoring stacks found"
mapfile -t PROM < <(kubectl get pods -A -l 'app.kubernetes.io/name in (prometheus,grafana)' --no-headers 2>/dev/null; kubectl get pods -A --no-headers 2>/dev/null | grep -Ei 'prometheus|grafana' | sort -u)
printf '%s\n' "${PROM[@]}" | sort -u | sed 's/^/   /' | head -20
[ ${#PROM[@]} -eq 0 ] && echo "   (none)"

echo; echo "== 1. pods that are not healthy in monitoring-looking namespaces"
bad=$(kubectl get pods -A --no-headers 2>/dev/null | grep -Ei 'prometheus|grafana|alertmanager|node-exporter|kube-state' | grep -Ev 'Running +[0-9]+ |Completed' | grep -Ev ' (1/1|2/2|3/3) +Running')
if [ -n "$bad" ]; then finding "monitoring pods not Running/Ready" "$bad"; else okline "no unhealthy monitoring pods"; fi

echo; echo "== 2. pending PVCs / StorageClass"
pend=$(kubectl get pvc -A --no-headers 2>/dev/null | awk '$3=="Pending"{print "   "$1"/"$2}')
if [ -n "$pend" ]; then
  finding "PersistentVolumeClaims stuck Pending (classic reason Prometheus/Grafana never start)" "$pend
    default StorageClass: $(kubectl get sc -o jsonpath='{range .items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")]}{.metadata.name}{end}' 2>/dev/null || true)
    Fix: provide a StorageClass/CSI driver, or install with emptyDir (this project does that automatically)."
else okline "no Pending PVCs"; fi

echo; echo "== 3. kubeadm control-plane components bound to localhost (their /metrics targets will be DOWN)"
for c in kube-controller-manager kube-scheduler; do
  args=$(kubectl -n kube-system get pods -l component=$c -o jsonpath='{.items[0].spec.containers[0].command}' 2>/dev/null)
  [ -z "$args" ] && continue
  if echo "$args" | grep -q -- '--bind-address=127.0.0.1'; then
    finding "$c listens on 127.0.0.1 only" "Prometheus cannot scrape it from a pod. This is kubeadm's default.
    Options: (a) do not scrape it (what this project does - touches nothing), or
             (b) set --bind-address=0.0.0.0 in its static pod manifest on EVERY manager (a control-plane change - not done automatically)."
  else okline "$c bind address is not 127.0.0.1"; fi
done
bp=$(kubectl -n kube-system get cm kube-proxy -o jsonpath='{.data.config\.conf}' 2>/dev/null | grep -E '^metricsBindAddress' )
[ -n "$bp" ] && echo "   kube-proxy: $bp   (empty/127.0.0.1:10249 => target DOWN from Prometheus)"

echo; echo "== 4. etcd topology"
etcd_args=$(kubectl -n kube-system get pods -l component=kube-apiserver -o jsonpath='{.items[0].spec.containers[0].command}' 2>/dev/null | tr ',' '\n' | grep -o 'etcd-servers=[^"]*' | head -1)
if echo "$etcd_args" | grep -qv '127.0.0.1'; then
  finding "etcd is EXTERNAL (${etcd_args#etcd-servers=}) - default kube-prometheus-stack etcd scraping cannot work" "There is no etcd pod to scrape and the endpoint needs a client certificate.
    Fix: ETCD_METRICS=true ETCD_ENDPOINTS=<ips> with scripts/etcd-client-secret.sh (opt-in; read-only on the control plane)."
else okline "etcd is stacked (pods in kube-system)"; fi

echo; echo "== 5. targets Prometheus itself reports down"
for ns in $(kubectl get pods -A -l app.kubernetes.io/name=prometheus --no-headers 2>/dev/null | awk '{print $1}' | sort -u); do
  svc=$(kubectl -n "$ns" get svc -o name 2>/dev/null | grep -E 'prometheus$|prometheus-operated' | head -1 | cut -d/ -f2)
  [ -z "$svc" ] && continue
  out=$(kubectl get --raw "/api/v1/namespaces/$ns/services/$svc:9090/proxy/api/v1/targets?state=active" 2>/dev/null) || continue
  echo "$out" | python3 -c '
import sys,json,collections
t=json.load(sys.stdin)["data"]["activeTargets"]
bad=[x for x in t if x["health"]!="up"]
print("   %d/%d targets down in namespace '"$ns"'" % (len(bad),len(t)))
for x in bad[:12]: print("     DOWN %-28s %s" % (x["labels"].get("job","?"), (x.get("lastError") or "")[:90]))' 2>/dev/null
done

echo; echo "== 6. Grafana datasource / ingress hints"
kubectl get ingress,httproute -A --no-headers 2>/dev/null | grep -Ei 'grafana|prometheus' | sed 's/^/   route: /' || true
kubectl get svc -A --no-headers 2>/dev/null | grep -Ei 'grafana' | sed 's/^/   svc: /'

echo
if [ $n_bad -eq 0 ]; then echo "No known kubeadm pitfalls found."; else echo "$n_bad finding(s) above. None of them were changed."; fi
