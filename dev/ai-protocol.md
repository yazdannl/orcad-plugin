# orcad AI coding: frontend <-> backend protocol (contract)

The AI assistant is an integrated **pi** coding agent (`@earendil-works/pi-coding-agent`,
pinned 0.87.1) running as `pi --mode rpc` under **Node.js LTS 24.21.0**, downloaded
from the official Node.js distribution and SHA-256 verified. Its data root is the
platform cache sibling `<cache>/orcad/ai` beside the OpenSCAD cache (overridable
with `ORCAD_AI_DATA_DIR` for tests). The agent works on one file, `model.scad`,
in an orcad-owned workspace below that root and renders it with the provisioned
OpenSCAD binary to check errors.

All messages go through the existing `Session.handle()` / `post()` channel. `id` is a
frontend-chosen string for a prompt run.

## Frontend -> backend

| message | meaning |
|---|---|
| `{type:"ai_status"}` | request current status (reply: `ai_status`) |
| `{type:"ai_setup"}` | download/install Node + pi (progress arrives as `ai_status` with `state:"installing"`) |
| `{type:"ai_config", source:"pi"\|"key", provider, model, api_key?, thinking?}` | `source:"pi"` = reuse the user's existing pi login/settings (~/.pi/agent); `source:"key"` = orcad-private pi config with the given provider API key (key stored 0600 in data_dir, never echoed back). `api_key` omitted = keep stored key. Reply: `ai_status` |
| `{type:"ai_prompt", id, text, code}` | run a prompt; `code` = current editor source, written to `model.scad` first |
| `{type:"ai_abort", id}` | abort the running prompt |
| `{type:"ai_reset"}` | start a fresh conversation |

## Backend -> frontend

```
{type:"ai_status", state:"missing"|"installing"|"ready"|"error", message, progress?:0..1,
 node_version?, pi_version?, busy:bool,
 config:{source:"pi"|"key", provider?, model?, thinking?, has_key:bool},
 models:[{provider, id, name}]}          // models available to pi with current config
{type:"ai_event", id, kind:"text", delta}                 // streamed assistant text
{type:"ai_event", id, kind:"thinking", delta}             // optional streamed reasoning
{type:"ai_event", id, kind:"tool", call_id, name, phase:"start"|"end", summary, is_error?}
{type:"ai_code", id, code}                                // model.scad changed (after edit/write)
{type:"ai_done", id, ok:bool, error?, code}               // run finished; code = final model.scad
```

Only one prompt runs at a time; `ai_prompt` while busy returns
`{type:"ai_done", id, ok:false, error:"busy"}`.

## Frontend behaviour

- "AI" panel in the Code view: chat transcript (markdown-ish text, collapsible tool rows),
  prompt box (Enter send, Shift+Enter newline), Stop, New chat.
- Setup card when `state` is `missing`/`installing`/`error` (Install button + progress);
  settings popover for source/provider/model/API key.
- On `ai_code`, replace the editor text; keep the pre-prompt source so the user can
  "Revert AI changes". On `ai_done ok`, trigger a normal preview render.
