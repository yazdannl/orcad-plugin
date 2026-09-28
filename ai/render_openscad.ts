import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

function bounds(path: string): number[][] | null {
  const data = readFileSync(path);
  if (data.length < 84) return null;
  const count = data.readUInt32LE(80);
  if (84 + count * 50 !== data.length) return null;
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  for (let offset = 84; offset < data.length; offset += 50) {
    for (let i = 0; i < 9; i++) {
      const axis = i % 3;
      const value = data.readFloatLE(offset + 12 + i * 4);
      min[axis] = Math.min(min[axis], value);
      max[axis] = Math.max(max[axis], value);
    }
  }
  return count ? min.map((value, axis) => [value, max[axis]]) : null;
}

function render(executable: string, args: string[], cwd: string, env: NodeJS.ProcessEnv,
  signal: AbortSignal): Promise<{ status: number | null; output: string; error?: string }> {
  return new Promise((resolvePromise, reject) => {
    if (signal.aborted) return reject(new Error("render cancelled"));
    const child = spawn(executable, args, { cwd, env, stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
    let output = "";
    let failure: string | undefined;
    const collect = (chunk: Buffer) => {
      if (output.length < 2 * 1024 * 1024) output += chunk.toString("utf8").slice(0, 2 * 1024 * 1024 - output.length);
    };
    child.stdout.on("data", collect);
    child.stderr.on("data", collect);
    child.on("error", (error) => { failure = error.message; });
    const abort = () => child.kill();
    signal.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(() => child.kill(), 120_000);
    child.on("close", (status) => {
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      if (signal.aborted) reject(new Error("render cancelled"));
      else resolvePromise({ status, output, error: failure });
    });
  });
}

export default function (pi: ExtensionAPI) {
  pi.on("tool_call", (event, context) => {
    if (event.toolName !== "read" && event.toolName !== "edit" && event.toolName !== "write") return;
    const input = event.input as { path?: string; file_path?: string };
    const path = input.path ?? input.file_path;
    if (typeof path !== "string" || resolve(context.cwd, path) !== resolve(context.cwd, "model.scad")) {
      return { block: true, reason: "orcad AI may access only model.scad" };
    }
  });
  pi.registerTool({
    name: "render_openscad",
    label: "Render OpenSCAD",
    description: "Compile the workspace model.scad with orcad's OpenSCAD/Manifold binary. Use after editing and iterate until there are no errors. Returns compiler errors and warnings and the printed-part bounding box in millimeters.",
    parameters: Type.Object({}),
    async execute(_id, _params, signal) {
      const executable = process.env.OPENSCAD_BIN;
      const library = process.env.OPENSCADPATH;
      if (!executable || !library) throw new Error("orcad did not configure the OpenSCAD runtime");
      const source = resolve(process.cwd(), "model.scad");
      const directory = mkdtempSync(join(process.cwd(), ".render-"));
      const output = join(directory, "model.stl");
      try {
        const backend = process.env.OPENSCAD_BACKEND_FLAG;
        const renderEnv = { ...process.env };
        for (const key of ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN", "OPENAI_API_KEY",
          "AZURE_OPENAI_API_KEY", "ANT_LING_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY", "COPILOT_GITHUB_TOKEN",
          "MISTRAL_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "XAI_API_KEY", "OPENROUTER_API_KEY",
          "NVIDIA_API_KEY", "FIREWORKS_API_KEY", "TOGETHER_API_KEY", "BASETEN_API_KEY", "HF_TOKEN", "AI_GATEWAY_API_KEY",
          "ZAI_API_KEY", "ZAI_CODING_CN_API_KEY", "OPENCODE_API_KEY", "RADIUS_API_KEY", "KIMI_API_KEY", "META_API_KEY",
          "MINIMAX_API_KEY", "MINIMAX_CN_API_KEY", "MOONSHOT_API_KEY", "QWEN_TOKEN_PLAN_API_KEY",
          "QWEN_TOKEN_PLAN_CN_API_KEY", "XIAOMI_API_KEY", "XIAOMI_TOKEN_PLAN_CN_API_KEY",
          "XIAOMI_TOKEN_PLAN_AMS_API_KEY", "XIAOMI_TOKEN_PLAN_SGP_API_KEY", "CLOUDFLARE_API_KEY",
          "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_BEARER_TOKEN_BEDROCK",
          "GOOGLE_CLOUD_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "NPM_TOKEN", "NODE_AUTH_TOKEN"]) {
          delete renderEnv[key];
        }
        const result = await render(executable, ["-o", output, "--export-format", "binstl", ...(backend ? [backend] : []), source], process.cwd(), renderEnv, signal);
        const diagnostics = result.output.split(/\r?\n/).map((line) => line.trim()).filter((line) =>
          /\b(ERROR|WARNING)\s*:/i.test(line) || /top level object is empty/i.test(line));
        const box = result.status === 0 && statSync(output, { throwIfNoEntry: false }) ? bounds(output) : null;
        const report = {
          ok: result.status === 0 && !result.error,
          errors: diagnostics.filter((line) => /\bERROR\s*:/i.test(line) || /top level object is empty/i.test(line)),
          warnings: diagnostics.filter((line) => /\bWARNING\s*:/i.test(line)),
          bbox_mm: box,
          message: result.error ?? (result.status !== 0 ? `OpenSCAD exited with code ${result.status}` : undefined),
        };
        return {
          content: [{ type: "text", text: JSON.stringify(report) }],
          details: report,
          isError: !report.ok,
        };
      } finally {
        rmSync(directory, { recursive: true, force: true });
      }
    },
  });
}
