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
import time
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
AI_MODELS = [
    {"provider": "openai", "id": "gpt-4.1-mini", "name": "GPT-4.1 mini"},
    {"provider": "openai", "id": "gpt-4.1", "name": "GPT-4.1"},
    {"provider": "anthropic", "id": "claude-sonnet-4", "name": "Claude Sonnet 4"},
]
AI_PROVIDERS = [
    {"id": "openai-codex", "name": "OpenAI ChatGPT", "methods": ["oauth"], "subscription": True, "status": "not configured"},
    {"id": "anthropic", "name": "Anthropic Claude Pro/Max", "methods": ["oauth", "api_key"], "subscription": True, "status": "not configured"},
    {"id": "github-copilot", "name": "GitHub Copilot", "methods": ["oauth"], "subscription": True, "status": "not configured"},
    {"id": "openai", "name": "OpenAI API", "methods": ["api_key"], "subscription": False, "status": "not configured"},
]
AI_CUSTOM_PROVIDERS = []
ai_lock = threading.Lock()
ai_state = "missing"
ai_busy_id = None
ai_auth_busy = False
ai_auth_request_id = None
ai_auth_provider = None
ai_auth_method = None
ai_cancelled = set()
ai_config = {"source": "pi", "provider": "openai", "model": "gpt-4.1-mini", "thinking": "medium", "has_key": False}
SAMPLE_SCAD = "$fn = 48;\n\ncylinder(h = 24, d = 32, center = true);\n"


def ai_status():
    with ai_lock:
        current_state = ai_state
        busy = ai_busy_id is not None or ai_auth_busy
        config = dict(ai_config)
    return {
        "type": "ai_status", "state": current_state,
        "message": "Mock pi agent for frontend development.",
        "progress": 1 if current_state == "ready" else 0,
        "node_version": "24.21.0" if current_state == "ready" else None,
        "pi_version": "0.87.1" if current_state == "ready" else None,
        "busy": busy, "auth_busy": ai_auth_busy, "config": config, "models": AI_MODELS,
        "providers": AI_PROVIDERS if config.get("source") != "pi" else [],
        "custom_providers": AI_CUSTOM_PROVIDERS if config.get("source") != "pi" else [],
    }


def install_mock_ai():
    global ai_state
    for progress in (0.2, 0.55, 0.85):
        time.sleep(0.35)
        with ai_lock:
            ai_state = "installing"
        post({**ai_status(), "state": "installing", "message": "Installing mock Node.js and pi…", "progress": progress})
    with ai_lock:
        ai_state = "ready"
    post(ai_status())


def run_mock_prompt(run_id, initial_code):
    global ai_busy_id
    try:
        for delta in ("I’ll make a simple centered cylinder ", "and keep the model editable in OpenSCAD."):
            time.sleep(0.35)
            with ai_lock:
                cancelled = run_id in ai_cancelled
            if cancelled:
                post({"type": "ai_done", "id": run_id, "ok": False, "error": "aborted", "code": initial_code})
                return
            post({"type": "ai_event", "id": run_id, "kind": "text", "delta": delta})
        post({"type": "ai_event", "id": run_id, "kind": "tool", "call_id": f"render-{run_id}",
              "name": "render_openscad", "phase": "start", "summary": "Checking the generated model"})
        time.sleep(0.3)
        with ai_lock:
            cancelled = run_id in ai_cancelled
        if cancelled:
            post({"type": "ai_done", "id": run_id, "ok": False, "error": "aborted", "code": initial_code})
            return
        post({"type": "ai_event", "id": run_id, "kind": "tool", "call_id": f"render-{run_id}",
              "name": "render_openscad", "phase": "end", "summary": "Render check passed"})
        post({"type": "ai_code", "id": run_id, "code": SAMPLE_SCAD})
        post({"type": "ai_done", "id": run_id, "ok": True, "code": SAMPLE_SCAD})
    finally:
        with ai_lock:
            ai_cancelled.discard(run_id)
            if ai_busy_id == run_id:
                ai_busy_id = None


def mock_auth_flow(provider, auth_type, request_id):
    if auth_type == "oauth":
        post({"type": "ai_auth_notice", "event": {"type": "auth_url",
              "url": "https://example.com/device", "instructions": "Open the provider page, then paste its redirect URL or code."}})
        time.sleep(0.25)
        prompt = {"type": "manual_code", "message": "Paste the final redirect URL or authorization code.",
                  "placeholder": "https://localhost/callback?...", "options": []}
    else:
        prompt = {"type": "secret", "message": f"Enter an API key for {provider}.", "placeholder": "API key", "options": []}
    post({"type": "ai_auth_prompt", "id": request_id, "prompt": prompt})


def handle_ai(message):
    global ai_state, ai_busy_id, ai_config, ai_auth_busy, ai_auth_request_id, ai_auth_provider, ai_auth_method
    global AI_PROVIDERS, AI_CUSTOM_PROVIDERS, AI_MODELS
    kind = message.get("type")
    if kind == "ai_status":
        post(ai_status())
    elif kind == "ai_setup":
        with ai_lock:
            if ai_state == "installing":
                return
            ai_state = "installing"
        post({**ai_status(), "state": "installing", "message": "Preparing mock pi installation…", "progress": 0.05})
        threading.Thread(target=install_mock_ai, daemon=True).start()
    elif kind == "ai_config":
        source = "pi" if message.get("source") == "pi" else "managed"
        with ai_lock:
            ai_config = {
                "source": source,
                "provider": message.get("provider") or "openai",
                "model": message.get("model") or "gpt-4.1-mini",
                "thinking": message.get("thinking") or "medium",
                "has_key": False,
            }
            ai_state = "ready"
        post(ai_status())
    elif kind == "ai_auth":
        provider, auth_type = message.get("provider"), message.get("auth_type")
        if message.get("action") == "logout":
            with ai_lock:
                for item in AI_PROVIDERS:
                    if item["id"] == provider:
                        item["status"] = "not configured"
            post({"type": "ai_auth_done", "action": "logout", "provider": provider, "ok": True})
            post(ai_status())
            return
        with ai_lock:
            if ai_config.get("source") != "managed" or ai_auth_busy:
                post({"type": "ai_auth_done", "ok": False, "message": "Use orcad-managed mode first."})
                return
            ai_auth_busy = True
            ai_auth_request_id = f"mock-auth-{time.monotonic_ns()}"
            ai_auth_provider, ai_auth_method = provider, auth_type
            request_id = ai_auth_request_id
        post(ai_status())
        threading.Thread(target=mock_auth_flow, args=(provider, auth_type, request_id), daemon=True).start()
    elif kind == "ai_auth_response":
        with ai_lock:
            if message.get("id") != ai_auth_request_id:
                return
            provider, auth_type = ai_auth_provider, ai_auth_method
            cancelled = message.get("cancelled") is True
            # The mock observes only whether a value was supplied; it never retains the value.
            configured = not cancelled and isinstance(message.get("value"), str) and bool(message.get("value"))
            ai_auth_busy = False
            ai_auth_request_id = ai_auth_provider = ai_auth_method = None
            for item in AI_PROVIDERS:
                if item["id"] == provider and configured:
                    item["status"] = "signed in" if auth_type == "oauth" else "key set"
        post({"type": "ai_auth_done", "action": "login", "provider": provider,
              "ok": configured, "cancelled": cancelled})
        post(ai_status())
    elif kind == "ai_auth_cancel":
        with ai_lock:
            ai_auth_busy = False
            ai_auth_request_id = ai_auth_provider = ai_auth_method = None
        post({"type": "ai_auth_done", "ok": False, "cancelled": True})
        post(ai_status())
    elif kind == "ai_provider_detect":
        post({"type": "ai_provider_detect_result", "id": message.get("id"), "ok": True,
              "models": [{"id": "qwen2.5-coder:7b", "name": "Qwen 2.5 Coder 7B"},
                         {"id": "llama3.2:latest", "name": "Llama 3.2"}]})
    elif kind == "ai_provider_save":
        provider = dict(message.get("provider") or {})
        provider_id = provider.get("id") or "-".join(provider.get("name", "local").lower().split())
        provider["id"] = provider_id
        with ai_lock:
            existing = next((item for item in AI_CUSTOM_PROVIDERS if item["id"] == provider_id), None)
            has_key = bool(message.get("api_key")) or (bool(existing and existing.get("has_key")) and not message.get("remove_key"))
            entry = {key: provider[key] for key in ("id", "name", "baseUrl", "api", "models") if key in provider}
            entry["has_key"] = has_key
            AI_CUSTOM_PROVIDERS = [item for item in AI_CUSTOM_PROVIDERS if item["id"] != provider_id] + [entry]
            AI_MODELS = [item for item in AI_MODELS if item["provider"] != provider_id]
            AI_MODELS.extend({"provider": provider_id, "id": model["id"], "name": model.get("name", model["id"])}
                             for model in provider.get("models", []) if isinstance(model, dict) and model.get("id"))
        post({"type": "ai_provider_result", "action": "save", "id": provider_id, "ok": True})
        post(ai_status())
    elif kind == "ai_provider_remove":
        provider_id = message.get("id")
        with ai_lock:
            AI_CUSTOM_PROVIDERS = [item for item in AI_CUSTOM_PROVIDERS if item["id"] != provider_id]
            AI_MODELS = [item for item in AI_MODELS if item["provider"] != provider_id]
        post({"type": "ai_provider_result", "action": "remove", "id": provider_id, "ok": True})
        post(ai_status())
    elif kind == "ai_prompt":
        run_id = message.get("id")
        with ai_lock:
            if ai_busy_id is not None or ai_auth_busy:
                post({"type": "ai_done", "id": run_id, "ok": False, "error": "busy", "code": message.get("code", "")})
                return
            ai_busy_id = run_id
            ai_state = "ready"
        threading.Thread(target=run_mock_prompt, args=(run_id, message.get("code", "")), daemon=True).start()
    elif kind == "ai_abort":
        with ai_lock:
            if message.get("id") == ai_busy_id:
                ai_cancelled.add(message.get("id"))
    elif kind == "ai_reset":
        with ai_lock:
            if ai_busy_id is not None:
                ai_cancelled.add(ai_busy_id)
            ai_busy_id = None
        post(ai_status())


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
        if isinstance(message, dict) and str(message.get("type", "")).startswith("ai_"):
            handle_ai(message)
        else:
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
