// Private stdio peer for an explicitly selected, independently installed Pi SDK.
import { Console } from "node:console";
import { randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { isAbsolute, join } from "node:path";
import { pathToFileURL } from "node:url";

const SDK_VERSION = "1.5.2";
const VERSION = 1;
const MAX_INPUT = 270336;
const MAX_OUTPUT = 2097152;
const ID = /^[0-9a-f]{32}$/;
const wire = process.stdout.write.bind(process.stdout);
// Pi or owner-selected extensions may use console for diagnostics.
globalThis.console = new Console(process.stderr);

function send(frame) {
  const line = JSON.stringify({ version: VERSION, ...frame }) + "\n";
  if (Buffer.byteLength(line) > MAX_OUTPUT) {
    throw new Error("Pi peer output exceeded the frame limit");
  }
  wire(line);
}

function response(id, disposition, reason = null, extra = {}) {
  send({ type: "response", id, disposition, reason, ...extra });
}

async function* commands() {
  let pending = Buffer.alloc(0);
  for await (const chunk of process.stdin) {
    pending = Buffer.concat([pending, chunk]);
    let end;
    while ((end = pending.indexOf(10)) >= 0) {
      if (end > MAX_INPUT) throw new Error("Pi peer command exceeded the frame limit");
      yield JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(pending.subarray(0, end)));
      pending = pending.subarray(end + 1);
    }
    if (pending.length > MAX_INPUT) throw new Error("Pi peer command exceeded the frame limit");
  }
  if (pending.length) throw new Error("Pi peer command ended before its delimiter");
}

const fields = {
  status: ["version", "id", "command"],
  submit: ["version", "id", "command", "text"],
  interrupt: ["version", "id", "command", "run_id"],
  close: ["version", "id", "command"],
  transcript_result: ["version", "id", "command", "run_id", "page"],
};

function validate(request) {
  if (
    !request || typeof request !== "object" || !Object.hasOwn(fields, request.command)
    || request.version !== VERSION || typeof request.id !== "string" || !ID.test(request.id)
    || Object.keys(request).length !== fields[request.command].length
    || !fields[request.command].every((key) => Object.hasOwn(request, key))
  ) {
    throw new Error("Malformed Pi peer command");
  }
  if (request.command === "submit" && (typeof request.text !== "string" || !request.text
      || Array.from(request.text).length > 16000)) throw new Error("Invalid Pi peer instruction");
  if (["interrupt", "transcript_result"].includes(request.command) && (typeof request.run_id !== "string"
      || !ID.test(request.run_id))) throw new Error("Invalid Pi peer run identity");
  if (request.command === "transcript_result" && request.page !== null
      && request.page?.kind !== "transcript") throw new Error("Invalid Pi transcript result");
}

async function transcriptTool(sdkRoot, currentRun, pending) {
  const { Type } = await import(pathToFileURL(join(sdkRoot, "node_modules/typebox/build/index.mjs")));
  const cursor = () => Type.Integer({ minimum: 0, maximum: Number.MAX_SAFE_INTEGER });
  return {
    name: "kinaiya_read_transcript",
    label: "Read Converse transcript",
    description: "Read an authorized page of this Converse transcript. Continue with the returned next_after and through_sequence while has_more is true.",
    parameters: Type.Object({
      sequence: Type.Optional(cursor()),
      byte_offset: Type.Optional(cursor()),
      through_sequence: Type.Optional(cursor()),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 128 })),
    }, { additionalProperties: false }),
    async execute(_toolCallId, params, signal) {
      const run = currentRun();
      if (!run || signal?.aborted || pending.size >= 4) {
        throw new Error("Converse transcript page is unavailable");
      }
      const id = randomBytes(16).toString("hex");
      const page = await new Promise((resolve) => {
        const cancel = () => finish(null);
        const timer = setTimeout(cancel, 6000);
        function finish(value) {
          pending.delete(id);
          clearTimeout(timer);
          signal?.removeEventListener("abort", cancel);
          resolve(value);
        }
        pending.set(id, { runId: run.id, finish });
        signal?.addEventListener("abort", cancel, { once: true });
        send({
          type: "transcript_request", id, run_id: run.id,
          sequence: params.sequence ?? 0, byte_offset: params.byte_offset ?? 0,
          through_sequence: params.through_sequence ?? null, limit: params.limit ?? 20,
        });
      });
      if (page === null) throw new Error("Converse transcript page is unavailable");
      const { entries, next_after, through_sequence, has_more } = page;
      return {
        content: [{ type: "text", text: JSON.stringify({ entries, next_after, through_sequence, has_more }) }],
        details: {},
      };
    },
  };
}

async function main() {
  const paths = process.argv.slice(2);
  const transcript = paths[0] === "--transcript";
  if (transcript) paths.shift();
  if (paths.length < 4 || paths.length > 5 || !paths.every(isAbsolute)) {
    throw new Error("Pi peer requires explicit absolute installation and session paths");
  }
  const [sdkRoot, cwd, agentDir, sessionDir, previousFile] = paths;
  const sdkPackage = join(sdkRoot, "node_modules", "@earendil-works", "pi-coding-agent");
  const manifest = JSON.parse(readFileSync(join(sdkPackage, "package.json"), "utf-8"));
  const entry = manifest.exports?.["."]?.import;
  if (manifest.name !== "@earendil-works/pi-coding-agent" || manifest.version !== SDK_VERSION
      || entry !== "./dist/index.js") {
    throw new Error("Unsupported Pi SDK version");
  }
  const { createAgentSession, SessionManager, SettingsManager } = await import(
    pathToFileURL(join(sdkPackage, entry))
  );
  const sessionManager = previousFile
    ? SessionManager.open(previousFile, sessionDir, cwd)
    : SessionManager.create(cwd, sessionDir);
  const settingsManager = SettingsManager.create(cwd, agentDir);
  const saved = sessionManager.buildSessionContext().model;
  const provider = saved?.provider ?? settingsManager.getDefaultProvider();
  const modelId = saved?.modelId ?? settingsManager.getDefaultModel();
  let active = null;
  const transcriptCalls = new Map();
  const customTools = transcript ? [await transcriptTool(sdkRoot, () => active, transcriptCalls)] : [];
  const { session } = await createAgentSession({
    cwd, agentDir, sessionManager, settingsManager, customTools,
  });
  try {
    // SDK 1.5.2 can select a model before extension registrations are applied (#721).
    if (provider && modelId && (session.model?.provider !== provider || session.model?.id !== modelId)) {
      const selected = session.modelRuntime.getModel(provider, modelId);
      if (!selected) throw new Error("The Pi-selected model is unavailable");
      await session.setModel(selected, { persist: false });
    }
  } catch {
    send({ type: "startup_refused", reason: "model_selection_unavailable" });
    session.dispose();
    process.exitCode = 1;
    return;
  }
  const issued = new Set();
  const busy = () => !!active || session.isStreaming || session.isCompacting
    || session.pendingMessageCount > 0;
  const event = (run, kind, detail = {}) => send({
    type: "event", run_id: run.id, kind, ...detail,
  });
  const unsubscribe = session.subscribe((item) => {
    const run = active;
    if (!run) return;
    if (item.type === "agent_start") {
      event(run, "running");
    } else if (item.type === "message_end" && item.message.role === "assistant") {
      run.stopReason = item.message.stopReason;
      const text = item.message.content.filter((part) => part.type === "text")
        .map((part) => part.text).join("");
      if (text) {
        if (Array.from(text).length <= 262144) event(run, "assistant_message", { text });
        else event(run, "output_unavailable");
      }
    } else if (item.type === "tool_execution_start" || item.type === "tool_execution_end") {
      const identifiers = [item.toolCallId, item.toolName];
      if (identifiers.some((id) => typeof id !== "string"
          || !/^[^\x00-\x1f\x7f]{1,256}$/.test(id))) {
        event(run, "output_unavailable");
        return;
      }
      event(run, "tool_activity", {
        tool_call_id: item.toolCallId,
        tool_name: item.toolName,
        phase: item.type === "tool_execution_start" ? "started" : item.isError ? "failed" : "finished",
      });
    } else if (item.type === "agent_settled") {
      // agent_end can be followed by retries, compaction recovery, or queued input.
      const kind = item.aborted ? "interrupted" : run.stopReason === "error" ? "failed" : "settled";
      event(run, kind);
      active = null;
    }
  });

  function submit({ id, text }) {
    if (busy()) return response(id, "refused", "busy");
    const run = { id, disposition: null, stopReason: null, interruptRequested: false };
    active = run;
    void session.prompt(text, {
      expandPromptTemplates: false,
      source: "rpc",
      preflightResult(disposition) {
        run.disposition = disposition;
        response(id, disposition === "started" ? "accepted" : "uncertain",
          disposition === "started" ? null : "input_handled");
      },
    }).then(() => {
      if (active !== run) return;
      if (!run.disposition) response(id, "uncertain", "preflight_not_observed");
      else if (run.disposition === "started") event(run, "uncertain");
      active = null;
    }, () => {
      if (active !== run) return;
      if (run.disposition === "started") event(run, "uncertain");
      else if (!run.disposition) response(id, "refused", "preflight_rejected");
      active = null;
    });
  }

  function interrupt({ id, run_id: runId }) {
    const run = active;
    if (run?.id !== runId || run.disposition !== "started") {
      return response(id, "refused", "run_changed");
    }
    if (run.interruptRequested) return response(id, "requested");
    run.interruptRequested = true;
    session.clearQueue();
    const stopping = session.abort();
    response(id, "requested");
    void stopping.catch(() => {
      if (active === run) event(run, "uncertain");
    });
  }

  try {
    send({ type: "ready", session_id: session.sessionId, sdk_version: SDK_VERSION, transcript });
    for await (const request of commands()) {
      validate(request);
      if (request.command === "transcript_result") {
        const original = transcriptCalls.get(request.id);
        if (original?.runId === request.run_id) original.finish(request.page);
        continue;
      }
      if (issued.has(request.id)) {
        response(request.id, "refused", "duplicate_request");
        continue;
      }
      if (issued.size >= 8192) throw new Error("Pi peer request identities exhausted");
      issued.add(request.id);
      if (request.command === "status") {
        response(request.id, "observed", null, {
          session_id: session.sessionId, busy: busy(), run_id: active?.id ?? null,
        });
      } else if (request.command === "submit") {
        submit(request);
      } else if (request.command === "interrupt") {
        interrupt(request);
      } else if (busy()) {
        response(request.id, "refused", "busy");
      } else {
        response(request.id, "stopping");
        break;
      }
    }
  } finally {
    unsubscribe();
    for (const call of transcriptCalls.values()) call.finish(null);
    if (busy()) await session.abort();
    session.dispose();
  }
}

await main().catch(() => {
  process.exitCode = 1;
});
