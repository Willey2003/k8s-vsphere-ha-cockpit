"""Long-running mode: rescan on an interval, serve /metrics, /api/report and a web view.

All links are relative, so the page works behind a path prefix such as /hostfail/.
"""
from __future__ import annotations

import html
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST
from prometheus_client.core import GaugeMetricFamily

from .scan import Config, scan
from .simulate import VERDICT_NAMES

log = logging.getLogger("hostfail")


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.inv = None
        self.rep = None
        self.ok = False
        self.error = ""
        self.last = 0.0
        self.duration = 0.0
        self.scans = 0


class ReportCollector:
    def __init__(self, state: State):
        self.s = state

    def collect(self):
        s = self.s
        with s.lock:
            inv, rep, ok, last, dur = s.inv, s.rep, s.ok, s.last, s.duration
        g = lambda n, d, labels=(): GaugeMetricFamily(n, d, labels=list(labels))
        up = g("hostfail_last_scan_success", "1 if the last scan of vCenter and Kubernetes succeeded")
        up.add_metric([], 1 if ok else 0)
        yield up
        ts = g("hostfail_last_scan_timestamp_seconds", "Unix time of the last scan attempt")
        ts.add_metric([], last)
        yield ts
        d = g("hostfail_scan_duration_seconds", "Duration of the last scan")
        d.add_metric([], dur)
        yield d
        if rep is None:
            return

        hi = g("hostfail_esxi_host_info", "ESXi hosts seen in vCenter", ["esxi_host", "vsphere_cluster", "connection_state", "maintenance"])
        for h in inv.hosts:
            hi.add_metric([h.name, h.cluster, h.connection_state, str(h.in_maintenance).lower()], 1)
        yield hi
        np = g("hostfail_node_placement", "Kubernetes node -> VM -> ESXi host", ["node", "vm", "esxi_host", "role"])
        for n in inv.nodes:
            np.add_metric([n.name, n.vm or "", n.esxi_host, "control-plane" if n.control_plane else "worker"], 1)
        yield np
        ep = g("hostfail_etcd_member_placement", "etcd member -> VM -> ESXi host", ["member", "vm", "esxi_host"])
        for m in inv.etcd:
            ep.add_metric([m.address, m.vm or "", m.esxi_host], 1)
        yield ep
        um = g("hostfail_unmapped_objects", "Nodes/etcd members that could not be matched to a VM")
        um.add_metric([], sum(1 for n in inv.nodes if not n.vm) + sum(1 for m in inv.etcd if not m.vm))
        yield um

        q = g("hostfail_host_failure_etcd_quorum_survives", "1 if etcd keeps quorum when this ESXi host fails", ["esxi_host"])
        a = g("hostfail_host_failure_api_survives", "1 if the Kubernetes API stays up when this ESXi host fails", ["esxi_host"])
        e = g("hostfail_host_failure_etcd_members_alive", "etcd members left when this ESXi host fails", ["esxi_host"])
        w = g("hostfail_host_failure_workloads_down", "Workloads losing every ready replica when this ESXi host fails", ["esxi_host"])
        p = g("hostfail_host_failure_pdb_violations", "PodDisruptionBudgets violated when this ESXi host fails", ["esxi_host"])
        c = g("hostfail_host_failure_capacity_ok", "1 if surviving nodes can absorb the evicted pods' requests", ["esxi_host"])
        v = g("hostfail_host_failure_verdict", "0=SAFE 1=DEGRADED 2=APP_OUTAGE 3=CLUSTER_DOWN", ["esxi_host", "verdict"])
        for r in rep.single:
            h = r.failed_hosts[0]
            q.add_metric([h], int(r.etcd_quorum))
            a.add_metric([h], int(r.api_available))
            e.add_metric([h], r.etcd_alive)
            w.add_metric([h], len(r.workloads_down))
            p.add_metric([h], len(r.pdb_violations))
            c.add_metric([h], int(r.capacity_ok))
            v.add_metric([h, r.verdict_name], r.verdict)
        yield from (q, a, e, w, p, c, v)
        if rep.double:
            dd = g("hostfail_double_failure_verdict", "Verdict for each pair of ESXi hosts failing together", ["esxi_hosts", "verdict"])
            for r in rep.double:
                dd.add_metric(["+".join(r.failed_hosts), r.verdict_name], r.verdict)
            yield dd
        spof = g("hostfail_single_points_of_failure", "ESXi hosts whose loss takes the whole cluster down")
        spof.add_metric([], len(rep.spof_hosts))
        yield spof
        etot = g("hostfail_etcd_members", "Discovered etcd members")
        etot.add_metric([], len(inv.etcd))
        yield etot
        dm = g("hostfail_drs_antiaffinity_missing", "1 if HA-critical VMs are not fully covered by an enabled DRS anti-affinity rule", ["group", "status"])
        for rec in rep.drs:
            dm.add_metric([rec["group"], rec["status"]], 0 if rec["status"] == "ok" else 1)
        yield dm


_COLOR = {0: "#1f9d55", 1: "#d69e2e", 2: "#dd6b20", 3: "#e53e3e"}


def render_html(state: State) -> str:
    with state.lock:
        inv, rep, ok, err, last = state.inv, state.rep, state.ok, state.error, state.last
    esc = html.escape
    if rep is None:
        body = f"<p>No scan yet. {esc(err)}</p>"
    else:
        cols = []
        for host, p in rep.placement.items():
            res = next((r for r in rep.single if r.failed_hosts == [host]), None)
            vcol = _COLOR.get(res.verdict, "#718096") if res else "#718096"
            chips = "".join(f'<div class="chip etcd">etcd · {esc(x)}</div>' for x in p["etcd"])
            chips += "".join(f'<div class="chip cp">{esc(x)}{" · VIP" if x == inv.vip_holder else ""}</div>' for x in p["control_plane"])
            chips += "".join(f'<div class="chip wk">{esc(x)}</div>' for x in p["workers"])
            reasons = "".join(f"<li>{esc(x)}</li>" for x in (res.reasons if res else []))
            cols.append(f'''<section class="host">
<header><h2>{esc(host)}</h2><span class="verdict" style="background:{vcol}">{esc(res.verdict_name) if res else "n/a"}</span></header>
<div class="chips">{chips or '<div class="muted">no cluster VMs</div>'}</div>
<p class="muted">If this host fails: etcd {res.etcd_alive if res else "-"}/{res.etcd_total if res else "-"}, apiservers {res.apiservers_alive if res else "-"}</p>
<ul>{reasons}</ul></section>''')
        drs = "".join(
            f'<tr><td>{esc(d["group"])}</td><td><b>{esc(d["status"])}</b></td><td>{esc(", ".join(d["vms"]))}</td>'
            f'<td><code>{esc(d.get("govc", "-"))}</code></td></tr>' for d in rep.drs) or '<tr><td colspan="4">nothing to separate</td></tr>'
        warns = "".join(f"<li>{esc(w)}</li>" for w in rep.warnings)
        if rep.spof_hosts:
            headline = f'<div class="banner bad">NOT highly available: losing <b>{esc(", ".join(rep.spof_hosts))}</b> takes the cluster down.</div>'
        else:
            headline = f'<div class="banner" style="background:{_COLOR[rep.worst]}">Worst single-host outcome: <b>{esc(VERDICT_NAMES[rep.worst])}</b></div>'
        body = f'''{headline}
<div class="grid">{"".join(cols)}</div>
<h3>DRS anti-affinity</h3><table><tr><th>group</th><th>status</th><th>VMs</th><th>suggested fix (review before running)</th></tr>{drs}</table>
{f"<h3>Warnings</h3><ul>{warns}</ul>" if warns else ""}'''
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last)) if last else "never"
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>kube-hostfail</title><meta http-equiv="refresh" content="30">
<style>
:root{{--bg:#0f1720;--card:#16212d;--fg:#e6edf3;--muted:#8b98a5;--line:#243243}}
@media (prefers-color-scheme: light){{:root{{--bg:#f5f7fa;--card:#fff;--fg:#1a202c;--muted:#5a6776;--line:#dfe5ec}}}}
body{{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:1200px;margin:0 auto;padding:20px 16px}}
h1{{font-size:20px;margin:0 0 4px}} .muted{{color:var(--muted)}}
.banner{{padding:12px 14px;border-radius:8px;color:#fff;margin:14px 0}} .banner.bad{{background:#e53e3e}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}}
.host{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}}
.host header{{display:flex;justify-content:space-between;align-items:center;gap:8px}} .host h2{{font-size:15px;margin:0;word-break:break-all}}
.verdict{{color:#fff;border-radius:999px;padding:2px 10px;font-size:12px;white-space:nowrap}}
.chips{{display:flex;flex-direction:column;gap:6px;margin:10px 0}}
.chip{{border-radius:6px;padding:5px 8px;font-size:13px;border-left:4px solid}}
.chip.etcd{{border-color:#805ad5;background:rgba(128,90,213,.12)}} .chip.cp{{border-color:#3182ce;background:rgba(49,130,206,.12)}} .chip.wk{{border-color:#718096;background:rgba(113,128,150,.12)}}
table{{width:100%;border-collapse:collapse;background:var(--card)}} td,th{{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}}
code{{font-size:12px;word-break:break-all}} ul{{margin:4px 0 0 18px;padding:0}}
</style></head><body><main>
<h1>kube-hostfail <span class="muted">· {esc(rep.cluster if rep else "")}</span></h1>
<div class="muted">Last scan {esc(stamp)} · {"ok" if ok else "FAILED: " + esc(err)} · <a href="api/report">JSON</a> · <a href="metrics">metrics</a></div>
{body}</main></body></html>'''


def serve(cfg: Config, port: int = 9877, interval: int = 60):
    state = State()
    registry = CollectorRegistry()
    registry.register(ReportCollector(state))

    def loop():
        while True:
            t0 = time.time()
            try:
                inv, rep = scan(cfg)
                with state.lock:
                    state.inv, state.rep, state.ok, state.error = inv, rep, True, ""
                log.info("scan ok: %d hosts, worst=%s, spof=%s", len(rep.hosts), VERDICT_NAMES[rep.worst], rep.spof_hosts)
            except Exception as ex:  # keep serving the last good report
                with state.lock:
                    state.ok, state.error = False, f"{type(ex).__name__}: {ex}"
                log.exception("scan failed")
            with state.lock:
                state.last, state.duration = t0, time.time() - t0
                state.scans += 1
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True).start()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            bare = self.path.split("?")[0]
            if bare and not bare.endswith("/") and bare.rsplit("/", 1)[-1] not in (
                    "metrics", "healthz", "readyz", "report"):
                # relative links on the page need a trailing slash when served under a prefix
                self.send_response(301)
                self.send_header("Location", bare + "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            path = self.path.split("?")[0].rstrip("/").rsplit("/", 1)[-1] if self.path not in ("/", "") else ""
            if path == "metrics":
                self._send(200, generate_latest(registry), CONTENT_TYPE_LATEST)
            elif path == "healthz":
                self._send(200, b"ok", "text/plain")
            elif path == "readyz":
                with state.lock:
                    ready = state.scans > 0
                self._send(200 if ready else 503, b"ready" if ready else b"waiting for first scan", "text/plain")
            elif path == "report":
                with state.lock:
                    rep, inv = state.rep, state.inv
                data = {"report": rep.to_dict() if rep else None, "inventory": inv.to_dict() if inv else None}
                self._send(200, json.dumps(data, indent=2).encode(), "application/json")
            else:
                self._send(200, render_html(state).encode(), "text/html; charset=utf-8")

    log.info("serving on :%d (scan every %ds)", port, interval)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
