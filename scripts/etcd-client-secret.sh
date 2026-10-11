#!/usr/bin/env bash
# OPTIONAL (ETCD_METRICS=true). Create the "etcd-client" secret Prometheus needs to scrape an external etcd.
# Run on a control-plane node as root (the apiserver's etcd client cert lives in /etc/kubernetes/pki), or pass paths:
#   sudo CA=/etc/kubernetes/pki/etcd/ca.crt CERT=/etc/kubernetes/pki/apiserver-etcd-client.crt \
#        KEY=/etc/kubernetes/pki/apiserver-etcd-client.key scripts/etcd-client-secret.sh
# This copies a client credential into a Secret in the observability namespace only. Treat that namespace as sensitive.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
CA=${CA:-/etc/kubernetes/pki/etcd/ca.crt}
CERT=${CERT:-/etc/kubernetes/pki/apiserver-etcd-client.crt}
KEY=${KEY:-/etc/kubernetes/pki/apiserver-etcd-client.key}
for f in "$CA" "$CERT" "$KEY"; do [ -r "$f" ] || die "cannot read $f (run as root on a control-plane node, or set CA/CERT/KEY)"; done
kubectl create ns "$NS_OBS" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS_OBS" create secret generic etcd-client --from-file=ca.crt="$CA" --from-file=tls.crt="$CERT" --from-file=tls.key="$KEY" \
  --dry-run=client -o yaml | kubectl apply -f -
say "secret etcd-client created in $NS_OBS"
