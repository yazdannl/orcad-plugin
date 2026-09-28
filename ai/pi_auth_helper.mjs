import { mkdir, chmod } from "node:fs/promises";
import { resolve, join } from "node:path";
import { pathToFileURL } from "node:url";
import { createInterface } from "node:readline";

const packageRoot = resolve(process.argv[2]);
const agentDir = resolve(process.argv[3]);
const action = process.argv[4];
const providerId = process.argv[5] || "";
const authType = process.argv[6] || "";
const authPath = join(agentDir, "auth.json");
const modelsPath = join(agentDir, "models.json");

function emit(record) {
  process.stdout.write(`${JSON.stringify(record)}\n`);
}

await mkdir(agentDir, { recursive: true, mode: 0o700 });
await chmod(agentDir, 0o700);
const { ModelRuntime } = await import(pathToFileURL(join(packageRoot, "dist/index.js")).href);
const runtime = await ModelRuntime.create({
  authPath,
  modelsPath,
  allowModelNetwork: false,
  refreshOnCreate: false,
});

if (action === "catalog") {
  const credentials = await runtime.listCredentials();
  const providers = runtime.getProviders().map((provider) => ({
    id: provider.id,
    name: provider.name,
    methods: [
      ...(provider.auth.oauth ? ["oauth"] : []),
      ...(provider.auth.apiKey?.login ? ["api_key"] : []),
    ],
    subscription: Boolean(provider.auth.oauth?.isSubscription),
    configured: Boolean(runtime.getProviderAuthStatus(provider.id)?.configured),
  })).filter((provider) => provider.methods.length);
  emit({ type: "catalog", providers, credentials });
  await new Promise((done) => process.stdout.write("", done));
  process.exit(0);
}

const controller = new AbortController();
const pending = new Map();
let sequence = 0;
const input = createInterface({ input: process.stdin });
input.on("line", (line) => {
  let response;
  try { response = JSON.parse(line); } catch { return; }
  if (response?.type === "cancel") {
    controller.abort();
    return;
  }
  const waiter = pending.get(response?.id);
  if (waiter) {
    pending.delete(response.id);
    waiter(response);
  }
});

function prompt(request) {
  return new Promise((resolvePrompt, rejectPrompt) => {
    if (controller.signal.aborted || request.signal?.aborted) {
      rejectPrompt(new Error("cancelled"));
      return;
    }
    const id = `auth-dialog-${++sequence}`;
    const abort = () => {
      pending.delete(id);
      rejectPrompt(new Error("cancelled"));
    };
    controller.signal.addEventListener("abort", abort, { once: true });
    request.signal?.addEventListener("abort", abort, { once: true });
    pending.set(id, (response) => {
      controller.signal.removeEventListener("abort", abort);
      request.signal?.removeEventListener("abort", abort);
      if (response.cancelled === true || typeof response.value !== "string") {
        controller.abort();
        rejectPrompt(new Error("cancelled"));
      } else {
        resolvePrompt(response.value);
      }
    });
    emit({
      type: "prompt",
      id,
      prompt: {
        type: request.type,
        message: request.message,
        placeholder: request.placeholder,
        options: request.type === "select" ? request.options : undefined,
      },
    });
  });
}

function notify(event) {
  if (event.type === "auth_url") {
    emit({ type: "notice", event: { type: event.type, url: event.url, instructions: event.instructions } });
  } else if (event.type === "device_code") {
    emit({ type: "notice", event: {
      type: event.type,
      userCode: event.userCode,
      verificationUri: event.verificationUri,
      intervalSeconds: event.intervalSeconds,
      expiresInSeconds: event.expiresInSeconds,
    } });
  } else if (event.type === "info" || event.type === "progress") {
    emit({ type: "notice", event: {
      type: event.type,
      message: event.message,
      links: event.type === "info" ? event.links : undefined,
    } });
  }
}

try {
  if (action === "logout") {
    await runtime.logout(providerId);
  } else if (action === "login") {
    const provider = runtime.getProvider(providerId);
    if (!provider || !provider.auth[authType === "oauth" ? "oauth" : "apiKey"] ||
        (authType === "api_key" && !provider.auth.apiKey?.login) ||
        !["oauth", "api_key"].includes(authType)) {
      emit({ type: "done", ok: false });
      await new Promise((done) => process.stdout.write("", done));
      process.exit(0);
    }
    await runtime.login(providerId, authType, { signal: controller.signal, prompt, notify });
  } else {
    emit({ type: "done", ok: false });
    await new Promise((done) => process.stdout.write("", done));
    process.exit(0);
  }
  await chmod(authPath, 0o600).catch(() => {});
  emit({ type: "done", ok: true });
} catch {
  emit({ type: "done", ok: false, cancelled: controller.signal.aborted });
}
await new Promise((done) => process.stdout.write("", done));
input.close();
process.exit(0);
