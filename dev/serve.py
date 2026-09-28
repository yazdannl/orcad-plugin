#!/usr/bin/env python3
"""Run the orcad page in a normal browser against the real plugin backend.

Simulates what OrcaSlicer provides: the window.orca bridge, the host theme
variables and the host's element-default stylesheet. Renders use the real
OpenSCAD pipeline (the same bootstrap/installer as inside OrcaSlicer).

    python3 dev/serve.py [--port 8765]   then open http://127.0.0.1:8765/?theme=dark|light
"""
import argparse
import json
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import orcad  # noqa: E402

THEMES = {
    "dark": ":root{--orca-bg:#2d2d31;--orca-fg:#efefef;--orca-muted:#9a9a9a;--orca-border:#4a4a51;"
            "--orca-accent:#009688;--orca-accent-fg:#fff;--orca-font:system-ui,sans-serif;color-scheme:dark}",
    "light": ":root{--orca-bg:#ffffff;--orca-fg:#262e30;--orca-muted:#6b6b6b;--orca-border:#dbdbdb;"
             "--orca-accent:#009688;--orca-accent-fg:#fff;--orca-font:system-ui,sans-serif;color-scheme:light}",
}
# Copied from OrcaSlicer's WebViewHostDialog::element_defaults_user_script().
HOST_DEFAULTS = (
    "html,body{background:var(--orca-bg);color:var(--orca-fg);font-family:var(--orca-font);font-size:13px;}"
    "body{margin:0;}h1,h2,h3,h4,h5,h6{color:var(--orca-fg);font-weight:600;}a{color:var(--orca-accent);}"
    "button{font:inherit;color:var(--orca-accent-fg);background:var(--orca-accent);border:1px solid var(--orca-accent);"
    "border-radius:4px;padding:5px 14px;cursor:pointer;}button:hover{filter:brightness(1.1);}"
    "button:disabled{opacity:.5;cursor:default;}input,select,textarea{font:inherit;color:var(--orca-fg);"
    "background:var(--orca-bg);border:1px solid var(--orca-border);border-radius:4px;padding:4px 8px;}"
    "input:focus,select:focus,textarea:focus{outline:none;border-color:var(--orca-accent);}"
)
BRIDGE = """<script>(function(){
  var handlers=[];
  window.orca={postMessage:function(d){fetch('/msg',{method:'POST',body:JSON.stringify(d===undefined?null:d)})},
               onMessage:function(cb){if(typeof cb==='function')handlers.push(cb)}};
  var es=new EventSource('/events');
  es.onmessage=function(e){var d=JSON.parse(e.data);for(var i=0;i<handlers.length;i++){try{handlers[i](d)}catch(x){console.error(x)}}};
})();</script>"""

clients: list[queue.Queue] = []
lock = threading.Lock()


def post(message):
    data = json.dumps(message)  # same JSON round trip as the host
    with lock:
        for client in clients:
            client.put(data)


session = orcad.Session(post)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            inbox: queue.Queue = queue.Queue()
            with lock:
                clients.append(inbox)
            try:
                while True:
                    self.wfile.write(f"data: {inbox.get()}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with lock:
                    clients.remove(inbox)
            return
        theme = parse_qs(url.query).get("theme", ["dark"])[0]
        head = (f'<style id="orca-host-theme-vars">{THEMES.get(theme, THEMES["dark"])}</style>'
                f'<script>document.documentElement.setAttribute("data-orca-theme","{theme}")</script>{BRIDGE}')
        page = orcad.page_html().replace("<head>", "<head>" + head, 1)
        before, _, after = page.rpartition("</body>")  # host styles land after the page's own
        page = f'{before}<style id="orca-plugin-defaults">{HOST_DEFAULTS}</style></body>{after}'
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        message = json.loads(self.rfile.read(length) or b"null")
        reply = session.handle(message)
        if reply is not None:
            post(reply)
        self.send_response(204)
        self.end_headers()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    orcad.scad.start_openscad_bootstrap()
    print(f"orcad dev server on http://127.0.0.1:{args.port}/  (?theme=light for the light theme)")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
