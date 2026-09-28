# orcad AI assistant protocol

The optional assistant runs Pi 0.87.1 with pinned Node.js LTS 24.21.0. All
messages travel over the existing `Session.handle()` / `post()` channel. Runtime,
workspace and the orcad-managed agent directory are below `<cache>/orcad/ai`.

## Provider authentication implementation

Pi's RPC extension-UI subprotocol supports extension `ctx.ui` calls, but it does
not make the built-in `/login` or `/logout` TUI commands available through RPC.
In Pi 0.87.1, `get_commands` did not list `login`, and sending `/login` through
the RPC `prompt` command was accepted as an ordinary model prompt. orcad
therefore uses Pi's public SDK `ModelRuntime.login()` / `logout()` API from the
small bundled `ai/pi_auth_helper.mjs` process. The helper passes Pi's
`AuthInteraction.prompt()` and `notify()` callbacks through the UI protocol;
Pi's `ModelRuntime` owns persistence to the active agent directory's `auth.json`.

The OAuth/API-key value entered for a Pi prompt is piped directly back to the
helper and is never added to the chat transcript, status, logs or stored page
settings. Only the requested sign-in URL/device code and generic progress are
shown transiently so the user can complete the flow. The final stored
credentials are never sent back to the page. In `Use my pi setup` mode the helper
uses `~/.pi/agent`; in `orcad-managed` mode it uses `<cache>/orcad/ai/private-agent`.
Sign-out calls Pi's `ModelRuntime.logout()` in the same directory.

Custom compatible endpoints are kept in the managed `models.json`. API keys are
stored in separate mode-0600 files under `private-agent/custom-keys/`; the JSON
contains only an environment-variable reference, never the key. Empty keys use
a harmless `orcad-local` placeholder for keyless/local-compatible servers.

## Frontend → backend

| Message | Meaning |
|---|---|
| `{type:"ai_status"}` | Request status. |
| `{type:"ai_setup"}` | Provision pinned Node + Pi; progress arrives in `ai_status`. |
| `{type:"ai_config", source:"pi"|"managed", provider?, model?, thinking?}` | Select the user's existing Pi setup or the isolated orcad-managed agent and active model. `busy` remains true while settings apply. |
| `{type:"ai_auth", action:"login"|"logout", provider, auth_type?}` | Start a Pi SDK OAuth/API-key sign-in or sign out a provider in the managed agent. `auth_type` is `oauth` or `api_key`. |
| `{type:"ai_auth_response", id, value? , cancelled?}` | Answer the current transient Pi auth prompt; select values must match a Pi-provided option. |
| `{type:"ai_auth_cancel"}` | Cancel an auth flow that is waiting on a provider/device poll. |
| `{type:"ai_provider_detect", id, baseUrl, api_key?}` | Optional `GET {baseUrl}/models`. A supplied key is used only as a bearer header for discovery and is not saved. |
| `{type:"ai_provider_save", provider:{id?,name,baseUrl,api,models:[{id,name}]}, api_key?, remove_key?}` | Add/edit an OpenAI-compatible provider; a blank key keeps an existing key. |
| `{type:"ai_provider_remove", id}` | Remove a managed custom provider and its saved key. |
| `{type:"ai_prompt", id, text, code}` | Run the model prompt against the current editor source. |
| `{type:"ai_abort", id}` / `{type:"ai_reset"}` | Stop the active prompt / begin a fresh chat. |

Custom endpoint API types supported in the form are `openai-completions`,
`openai-responses`, `anthropic-messages`, and `google-generative-ai`. Plain HTTP
is limited to localhost/private endpoints; remote endpoints require HTTPS.

## Backend → frontend

```text
{type:"ai_status", state:"missing"|"installing"|"ready"|"error", message, progress?,
 node_version?, pi_version?, busy, auth_busy,
 config:{source:"pi"|"managed", provider?, model?, thinking?, has_key},
 models:[{provider,id,name}],
 providers:[{id,name,methods:["oauth"|"api_key"],subscription,status:"signed in"|"key set"|"not configured"}],
 custom_providers:[{id,name,baseUrl,api,models:[{id,name}],has_key}]}
{type:"ai_auth_prompt", id, prompt:{type:"text"|"secret"|"manual_code"|"select",message,placeholder?,options?:[{id,label,description?}]}}
{type:"ai_auth_notice", event:{type:"info"|"progress"|"auth_url"|"device_code", ...}}
{type:"ai_auth_done", action?, provider?, ok, cancelled?, message?}
{type:"ai_provider_detect_result", id, ok, models?:[{id,name}], error?}
{type:"ai_provider_result", action:"save"|"remove", id, ok, error?}
{type:"ai_event", id, kind:"text"|"thinking"|"tool", ...}
{type:"ai_code", id, code}
{type:"ai_done", id, ok, error?, code}
```

Neither `ai_status` nor any result includes API keys, OAuth tokens, or contents of
`auth.json`. `models` is populated from Pi RPC `get_available_models`; custom
provider files are written only below the private agent directory. One prompt
runs at a time; prompts sent while work is active receive `ai_done` with
`error:"busy"`.

## Frontend behavior

- Code view has the streamed chat, tool rows, Stop/New chat/Revert, and a settings
  section for mode/provider/model, Pi sign-in/API key, and custom endpoints.
- Sign-in dialogs support Pi select/text/secret/manual-code prompts and transient
  authorization URL/device-code notices. Passwords and pasted redirect values are
  cleared from component state immediately after submission.
- Provider/model selection comes from `get_available_models`. `auth.json` is
  managed only by Pi; built-in credentials are not returned to the page.
- Custom model discovery fills the form from the compatible endpoint's `/models`
  response. The backend validates endpoints, caps response size and refuses
  redirects.
- On `ai_code`, replace editor text while preserving the pre-prompt source for
  Revert. On successful `ai_done`, trigger the regular local OpenSCAD preview.
