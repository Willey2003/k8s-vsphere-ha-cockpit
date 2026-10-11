#!/usr/bin/env bash
# No LoadBalancer? Reach the whole cockpit through ONE local port via kubectl (works over an SSH tunnel too).
#   scripts/port-forward.sh            -> http://localhost:8080/
# From your laptop through the bastion:
#   ssh -L 8080:localhost:8080 <user>@<bastion-host>   then run this script on the bastion
set -euo pipefail
. "$(dirname "$0")/lib.sh"
PORT=${1:-8080}
say "forwarding gateway service ${GATEWAY_NAME}-istio -> http://localhost:$PORT/   (Ctrl-C to stop)"
exec kubectl -n "$GATEWAY_NAMESPACE" port-forward --address 127.0.0.1 "svc/${GATEWAY_NAME}-istio" "$PORT:80"
