#!/usr/bin/env python3
"""Tiny 3-service demo shop that gives Kiali a graph and Jaeger real traces.

  ROLE=frontend  GET /shop/            -> calls catalog, renders HTML
  ROLE=catalog   GET /items            -> calls pricing once per item
  ROLE=pricing   GET /price?item=x     -> answers, with optional latency / errors
  ROLE=loadgen   loops GET frontend    -> keeps the graphs alive

Istio's sidecars create the spans; the app only has to forward the trace headers,
which is what every service does below. Standard library only.
"""
import json
import os
import random
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROLE = os.environ.get("ROLE", "frontend")
VERSION = os.environ.get("VERSION", "v1")
CATALOG = os.environ.get("CATALOG_URL", "http://catalog:8080")
PRICING = os.environ.get("PRICING_URL", "http://pricing:8080")
LATENCY_MS = int(os.environ.get("LATENCY_MS", "0"))
FAIL_RATE = float(os.environ.get("FAIL_RATE", "0"))
TRACE_HEADERS = ("x-request-id", "traceparent", "tracestate", "x-b3-traceid", "x-b3-spanid",
                 "x-b3-parentspanid", "x-b3-sampled", "x-b3-flags", "b3", "x-ot-span-context")
ITEMS = ["vmxnet3-nic", "pvscsi-controller", "esxi-host", "etcd-member", "k8s-node"]


def call(url, incoming):
    headers = {h: incoming[h] for h in TRACE_HEADERS if incoming.get(h)}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.load(r)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.endswith("/healthz"):
            return self.send(200, "ok", "text/plain")
        inc = {k.lower(): v for k, v in self.headers.items()}
        if ROLE == "pricing":
            if LATENCY_MS:
                time.sleep(random.uniform(0, LATENCY_MS) / 1000)
            if random.random() < FAIL_RATE:
                return self.send(503, json.dumps({"error": "pricing backend unavailable"}))
            item = self.path.split("item=")[-1] if "item=" in self.path else "?"
            return self.send(200, json.dumps({"item": item, "price": round(10 + sum(map(ord, item)) % 90 + random.random(), 2),
                                              "version": VERSION}))
        if ROLE == "catalog":
            out = []
            for item in ITEMS:
                try:
                    out.append(call(f"{PRICING}/price?item={item}", inc))
                except Exception:
                    out.append({"item": item, "price": None})
            return self.send(200, json.dumps({"items": out, "version": VERSION}))
        # frontend
        try:
            data = call(f"{CATALOG}/items", inc)
            rows = "".join(f"<tr><td>{i['item']}</td><td>{'%.2f' % i['price'] if i['price'] is not None else 'n/a'}</td></tr>"
                           for i in data["items"])
            err = ""
        except Exception as ex:
            rows, err = "", f"<p style='color:#e5484d'>catalog error: {ex}</p>"
        page = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Demo shop</title><body style="font:15px system-ui;max-width:640px;margin:32px auto;padding:0 16px">
<h1>Infra parts shop</h1><p>frontend {VERSION} → catalog → pricing (trace id {inc.get('x-b3-traceid', inc.get('traceparent', '-'))})</p>
{err}<table border=1 cellpadding=6 style="border-collapse:collapse">{rows}</table>
<p><small>Open Kiali to see this request graph, Jaeger to see its trace.</small></p></body>"""
        return self.send(200, page, "text/html; charset=utf-8")


def loadgen():
    url = os.environ.get("TARGET", "http://frontend:8080/shop/")
    rps = float(os.environ.get("RPS", "2"))
    while True:
        try:
            urllib.request.urlopen(url, timeout=5).read()
        except Exception:
            pass
        time.sleep(1 / rps)


if __name__ == "__main__":
    if ROLE == "loadgen":
        loadgen()
    port = int(os.environ.get("PORT", "8080"))
    print(f"{ROLE} {VERSION} on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
