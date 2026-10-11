#!/usr/bin/env python3
"""Generate docs/architecture.svg (no dependencies). Run: python3 docs/gen_diagram.py"""
W, H = 1240, 700
out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="system-ui,Segoe UI,sans-serif" font-size="13">',
       '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
       '<path d="M0 0L10 5L0 10z" fill="#64748b"/></marker></defs>',
       f'<rect width="{W}" height="{H}" fill="#0f172a"/>',
       '<text x="24" y="34" fill="#f1f5f9" font-size="20" font-weight="700">HA Cockpit: Kubernetes on vSphere, seen from both sides</text>']


def box(x, y, w, h, title, lines=(), fill="#1e293b", stroke="#334155", tcolor="#e2e8f0"):
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="{stroke}"/>')
    out.append(f'<text x="{x+12}" y="{y+22}" fill="{tcolor}" font-weight="600">{title}</text>')
    for i, l in enumerate(lines):
        out.append(f'<text x="{x+12}" y="{y+42+i*18}" fill="#94a3b8">{l}</text>')


def chip(x, y, w, label, color):
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="24" rx="6" fill="{color}" fill-opacity=".22" stroke="{color}"/>')
    out.append(f'<text x="{x+10}" y="{y+16}" fill="#e2e8f0">{label}</text>')


def arrow(x1, y1, x2, y2, label=""):
    out.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#64748b" stroke-width="1.6" marker-end="url(#a)"/>')
    if label:
        out.append(f'<text x="{(x1+x2)/2+6}" y="{(y1+y2)/2-6}" fill="#94a3b8" font-size="12">{label}</text>')


# vSphere side
box(24, 60, 560, 300, "vSphere  (read-only view)", fill="#0b1220")
hosts = [("esx001", ["etcd-1", "manager-1", "worker-1"]), ("esx002", ["etcd-2", "manager-2", "worker-2"]), ("esx003", ["etcd-3", "manager-3", "worker-3"])]
for i, (h, vms) in enumerate(hosts):
    x = 40 + i * 180
    box(x, 100, 170, 150, h, fill="#172033")
    for j, v in enumerate(vms):
        chip(x + 10, 130 + j * 32, 150, v, "#a78bfa" if v.startswith("etcd") else "#60a5fa" if v.startswith("manager") else "#94a3b8")
out.append('<text x="40" y="285" fill="#94a3b8">DRS / vMotion can move these VMs at any time.</text>')
out.append('<text x="40" y="305" fill="#94a3b8">Kubernetes never sees which host a node is on.</text>')
out.append('<text x="40" y="335" fill="#fbbf24">Two etcd VMs on one host = one host failure from losing quorum.</text>')

# kube-hostfail
box(650, 60, 290, 150, "kube-hostfail", ["maps node / etcd member to VM, host", "simulates every host failure", "checks DRS anti-affinity rules", "prints the fix, never applies it"], stroke="#22c55e", tcolor="#86efac")
arrow(584, 150, 650, 150)
out.append('<text x="592" y="140" fill="#94a3b8" font-size="11">vCenter</text><text x="596" y="172" fill="#94a3b8" font-size="11">read-only</text>')
box(650, 230, 290, 70, "Kubernetes API (read-only)", ["nodes, pods, PDBs, VIP lease"])
arrow(795, 230, 795, 210)

# observability
box(990, 60, 226, 150, "Prometheus + Grafana", ["hostfail_* metrics + alerts", "verdict-per-host dashboard", "kubeadm-safe scrape config"], stroke="#f97316", tcolor="#fdba74")
arrow(940, 135, 990, 135)
out.append('<text x="944" y="125" fill="#94a3b8" font-size="11">/metrics</text>')

# mesh
box(650, 330, 566, 130, "Istio service mesh", ["demo shop: frontend, catalog, pricing v1/v2", "Envoy sidecars to Prometheus (metrics)  and  Jaeger (traces)", "Kiali draws the live request graph"], stroke="#38bdf8", tcolor="#7dd3fc")
chip(670, 420, 90, "Kiali", "#38bdf8"); chip(770, 420, 90, "Jaeger", "#38bdf8"); chip(870, 420, 110, "Demo shop", "#38bdf8")
arrow(1103, 330, 1103, 210)

# gateway + launcher
box(24, 490, 1192, 190, "One Gateway (Gateway API), path-routed, one port", fill="#0b1220", stroke="#6366f1", tcolor="#a5b4fc")
tiles = ["/  Launcher", "/hostfail", "/grafana", "/prometheus", "/kiali", "/jaeger", "/shop"]
for i, t in enumerate(tiles):
    chip(44 + i * 166, 526, 150, t, "#6366f1")
out.append('<text x="44" y="586" fill="#94a3b8">Launcher builds its tiles from the HTTPRoutes in the cluster: publish an app, a tile appears. Each tile has a live health dot.</text>')
out.append('<text x="44" y="608" fill="#94a3b8">Reached over MetalLB, NodePort, or kubectl port-forward through the bastion.</text>')
out.append('<text x="44" y="648" fill="#64748b">Nothing here writes to vCenter. Sidecars are injected only into the demo namespace.</text>')
out.append('</svg>')
open(__file__.replace("gen_diagram.py", "architecture.svg"), "w").write("\n".join(out))
print("wrote architecture.svg")
