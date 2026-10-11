#!/usr/bin/env bash
# Verify the whole stack end to end, read-only. Prints the URL to open.
set -uo pipefail
. "$(dirname "$0")/lib.sh"
need kubectl; need curl
fail=0
check() { if eval "$2" >/dev/null 2>&1; then printf '  \033[32mok\033[0m   %s\n' "$1"; else printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=1; fi; }

say "workloads"
for p in "$NS_OBS:statefulset/prometheus-kps-prometheus" "$NS_OBS:deploy/kps-grafana" "$NS_ISTIO:deploy/istiod" "$NS_ISTIO:deploy/jaeger" \
         "$NS_ISTIO:deploy/kiali" "$NS_SYS:deploy/kube-hostfail" "$NS_SYS:deploy/launcher" "$NS_DEMO:deploy/frontend"; do
  check "${p#*:} ready" "kubectl -n ${p%%:*} rollout status ${p#*:} --timeout=5s"
done
check "Gateway programmed" "kubectl -n $GATEWAY_NAMESPACE get gateway $GATEWAY_NAME -o jsonpath='{.status.conditions[?(@.type==\"Programmed\")].status}' | grep -q True"
check "routes accepted ($(kubectl get httproute -A -l app.kubernetes.io/part-of=ha-cockpit --no-headers 2>/dev/null | wc -l))" \
      "! kubectl get httproute -A -l app.kubernetes.io/part-of=ha-cockpit -o jsonpath='{range .items[*].status.parents[*].conditions[?(@.type==\"Accepted\")]}{.status}{\"\\n\"}{end}' | grep -q False"

say "Prometheus targets (through the API server proxy - no gateway needed)"
tj=$(kubectl get --raw "/api/v1/namespaces/$NS_OBS/services/kps-prometheus:9090/proxy/prometheus/api/v1/targets?state=active" 2>/dev/null)
if [ -n "$tj" ]; then
  echo "$tj" | python3 -c '
import sys, json, collections
d = json.load(sys.stdin)["data"]["activeTargets"]
c = collections.Counter((t["labels"].get("job","?"), t["health"]) for t in d)
for (job, h), n in sorted(c.items()):
    flag = "ok  " if h == "up" else "DOWN"
    print("  %s %s: %d %s" % (flag, job, n, h))
down = [t["scrapeUrl"] for t in d if t["health"] != "up"]
sys.exit(1 if down else 0)' || fail=1
else
  printf '  \033[33mskip\033[0m  could not query Prometheus\n'
fi
for m in hostfail_single_points_of_failure istio_requests_total; do
  n=$(kubectl get --raw "/api/v1/namespaces/$NS_OBS/services/kps-prometheus:9090/proxy/prometheus/api/v1/query?query=count($m)" 2>/dev/null | python3 -c 'import sys,json;r=json.load(sys.stdin)["data"]["result"];print(r[0]["value"][1] if r else 0)' 2>/dev/null || echo 0)
  check "metric $m present ($n series)" "[ \"${n:-0}\" != 0 ]"
done

say "URL"
ip=$(kubectl -n "$GATEWAY_NAMESPACE" get svc "${GATEWAY_NAME}-istio" -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null)
if [ -n "$ip" ]; then echo "  open  http://${COCKPIT_HOST:-$ip}/"; else echo "  no external IP yet. Try: scripts/port-forward.sh"; fi
[ $fail = 0 ] && say "all checks passed" || { warn "some checks failed"; exit 1; }
