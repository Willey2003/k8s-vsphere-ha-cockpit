#!/usr/bin/env python3
"""App Launcher - one page, one tile per app, built from Gateway API HTTPRoutes.

No config to edit: every HTTPRoute in the cluster becomes a tile. Optional annotations:
  cockpit.io/title        tile title             (default: route name)
  cockpit.io/description  one line of text
  cockpit.io/icon         an emoji or 1-3 letters
  cockpit.io/group        section heading        (default: namespace)
  cockpit.io/health-path  path probed for the status dot (default: the route's path)
  cockpit.io/hide         "true" to leave it out
Extra tiles (e.g. apps on another cluster) can be given as JSON in EXTRA_APPS.

Health is probed *through the gateway* (GATEWAY_URL), i.e. exactly the path a user's
click takes. Standard library only - runs from a ConfigMap on python:3-alpine.
"""
import html
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SA = os.environ.get("SA_DIR", "/var/run/secrets/kubernetes.io/serviceaccount")
API = os.environ.get("K8S_API", "https://kubernetes.default.svc")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "").rstrip("/")
TITLE = os.environ.get("LAUNCHER_TITLE", "HA Cockpit")
INTERVAL = int(os.environ.get("PROBE_INTERVAL", "15"))
PREFIX = "cockpit.io/"

STATE = {"apps": [], "updated": 0, "error": ""}
LOCK = threading.Lock()


def k8s_get(path):
    token = open(f"{SA}/token").read().strip()
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt") if API.startswith("https") else None
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
        return json.load(r)


def route_to_app(rt):
    md = rt["metadata"]
    ann = md.get("annotations") or {}
    if ann.get(PREFIX + "hide", "").lower() == "true":
        return None
    path = "/"
    for rule in rt.get("spec", {}).get("rules") or []:
        for m in rule.get("matches") or []:
            p = (m.get("path") or {}).get("value")
            if p:
                path = p
                break
        if path != "/":
            break
    hosts = [h for h in rt.get("spec", {}).get("hostnames") or [] if "*" not in h]
    host = hosts[0] if hosts else ""
    url = path if path.endswith("/") or "." in path.rsplit("/", 1)[-1] else path + "/"
    probe_path = ann.get(PREFIX + "health-path", url)
    base = GATEWAY_URL or (f"http://{host}" if host else "")
    title = ann.get(PREFIX + "title") or md["name"]
    return {
        "title": title,
        "description": ann.get(PREFIX + "description", ""),
        "icon": ann.get(PREFIX + "icon") or title[:2].upper(),
        "group": ann.get(PREFIX + "group") or md["namespace"],
        "url": f"http://{host}{url}" if host else url,
        "probe": base + probe_path if base else "",
        "probe_host": host,
        "source": f"HTTPRoute {md['namespace']}/{md['name']}",
        "status": "unknown", "code": None, "ms": None,
    }


def probe(app):
    url = app["probe"]
    if not url.startswith("http"):
        return "unknown", None, None
    t0 = time.time()
    try:
        headers = {"User-Agent": "cockpit-launcher"}
        if app.get("probe_host"):
            headers["Host"] = app["probe_host"]      # probe through the gateway, as a browser would
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=4) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        return "down", None, None
    ms = int((time.time() - t0) * 1000)
    # 401/403 mean the app is up and asking for a login
    return ("up" if code < 500 else "down"), code, ms


def refresh():
    while True:
        try:
            apps = []
            try:
                routes = k8s_get("/apis/gateway.networking.k8s.io/v1/httproutes")["items"]
                apps = [a for a in (route_to_app(r) for r in routes) if a]
                err = ""
            except Exception as ex:
                err = f"cannot list HTTPRoutes: {ex}"
            for extra in json.loads(os.environ.get("EXTRA_APPS", "[]") or "[]"):
                apps.append({"title": extra["title"], "description": extra.get("description", ""),
                             "icon": extra.get("icon", extra["title"][:2].upper()),
                             "group": extra.get("group", "External"), "url": extra["url"],
                             "probe": extra.get("probe", ""), "probe_host": "", "source": "EXTRA_APPS",
                             "status": "unknown", "code": None, "ms": None})
            for a in apps:
                a["status"], a["code"], a["ms"] = probe(a)
            apps.sort(key=lambda a: (a["group"], a["title"].lower()))
            with LOCK:
                STATE.update(apps=apps, updated=time.time(), error=err)
        except Exception as ex:  # never let the loop die
            with LOCK:
                STATE["error"] = str(ex)
        time.sleep(INTERVAL)


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
:root{{--bg:#0e141b;--card:#151e28;--fg:#e8eef4;--muted:#8a97a6;--line:#233140;--up:#22a35a;--down:#e5484d;--unk:#8a97a6;--accent:#4c9aff}}
@media (prefers-color-scheme: light){{:root{{--bg:#f4f6f9;--card:#fff;--fg:#17202b;--muted:#5b6876;--line:#dde3ea}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1180px;margin:0 auto;padding:24px 16px 40px}}
header{{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:18px}}
h1{{font-size:22px;margin:0}} .muted{{color:var(--muted)}}
input{{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:9px 12px;min-width:240px;font:inherit}}
h2{{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:22px 0 10px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}}
a.tile{{display:flex;gap:12px;align-items:flex-start;padding:14px;background:var(--card);border:1px solid var(--line);border-radius:12px;color:inherit;text-decoration:none;transition:border-color .15s,transform .15s}}
a.tile:hover{{border-color:var(--accent);transform:translateY(-1px)}}
.icon{{flex:none;width:42px;height:42px;border-radius:10px;display:grid;place-items:center;background:rgba(76,154,255,.14);font-weight:700;font-size:18px}}
.t{{font-weight:600;display:flex;align-items:center;gap:8px}} .d{{color:var(--muted);font-size:13px;margin-top:2px}}
.dot{{width:9px;height:9px;border-radius:50%;background:var(--unk);flex:none}} .dot.up{{background:var(--up)}} .dot.down{{background:var(--down)}}
.meta{{font-size:12px;color:var(--muted);margin-top:6px}} .err{{color:var(--down)}}
</style></head><body><main>
<header><div><h1>{title}</h1><div class="muted" id="sub">loading…</div></div>
<input id="q" placeholder="Filter apps…" autocomplete="off"></header>
<div id="apps"></div>
<script>
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c]));
let data={{apps:[]}};
function draw(){{
  const q=document.getElementById('q').value.toLowerCase(); const groups={{}};
  for(const a of data.apps){{ if(q && !(a.title+a.description+a.group).toLowerCase().includes(q)) continue; (groups[a.group]??=[]).push(a); }}
  let h=''; for(const [g,list] of Object.entries(groups)){{
    h+=`<h2>${{esc(g)}}</h2><div class="grid">`;
    for(const a of list){{ const st=a.status==='up'?`${{a.code}} · ${{a.ms}} ms`:a.status==='down'?(a.code?`HTTP ${{a.code}}`:'unreachable'):'not probed';
      h+=`<a class="tile" href="${{esc(a.url)}}" target="_blank" rel="noopener" title="${{esc(a.source)}}"><div class="icon">${{esc(a.icon)}}</div><div><div class="t"><span class="dot ${{esc(a.status)}}"></span>${{esc(a.title)}}</div><div class="d">${{esc(a.description)}}</div><div class="meta">${{esc(st)}}</div></div></a>`; }}
    h+='</div>'; }}
  document.getElementById('apps').innerHTML=h||'<p class="muted">No apps found. Publish one with an HTTPRoute.</p>';
  const up=data.apps.filter(a=>a.status==='up').length;
  document.getElementById('sub').innerHTML=`${{up}}/${{data.apps.length}} apps up · discovered from HTTPRoutes`+(data.error?` · <span class="err">${{esc(data.error)}}</span>`:'');
}}
async function load(){{ try{{ const r=await fetch('api/apps'); data=await r.json(); }}catch(e){{ data.error='launcher API unreachable'; }} draw(); }}
document.getElementById('q').addEventListener('input',draw); load(); setInterval(load,10000);
</script></main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p.endswith("/api/apps"):
            with LOCK:
                self._send(200, json.dumps(STATE), "application/json")
        elif p.endswith("/healthz"):
            self._send(200, "ok", "text/plain")
        else:
            self._send(200, PAGE.format(title=html.escape(TITLE)), "text/html; charset=utf-8")


if __name__ == "__main__":
    threading.Thread(target=refresh, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    print(f"launcher on :{port}, gateway={GATEWAY_URL or '-'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
