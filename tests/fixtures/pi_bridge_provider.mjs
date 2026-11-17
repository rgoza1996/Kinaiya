// Deterministic provider for the real Pi SDK integration test.
import { createAssistantMessageEventStream } from "/sdk/node_modules/@earendil-works/pi-ai/dist/index.js";

export default function fixture(pi) {
  pi.registerCommand("fixture-command", {
    description: "Verify that forwarded text cannot invoke a Pi command",
    handler: async () => { throw new Error("A forwarded instruction invoked a Pi command"); },
  });
  pi.registerProvider("kinaiya-bridge-fixture", {
    baseUrl: "http://127.0.0.1:1",
    apiKey: "synthetic",
    api: "kinaiya-bridge-fixture",
    models: [{
      id: "fixture", name: "Fixture", input: ["text"], reasoning: false,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 32000, maxTokens: 1024,
    }],
    streamSimple(model, context, options) {
      const stream = createAssistantMessageEventStream();
      const input = context.messages.filter((message) => message.role === "user").at(-1).content;
      const text = typeof input === "string" ? input
        : input.filter((part) => part.type === "text").map((part) => part.text).join("");
      function finish(stopReason, content = stopReason === "stop" ? [{ type: "text", text }] : []) {
        const result = {
          role: "assistant",
          content,
          api: model.api, provider: model.provider, model: model.id,
          usage: { input: 0, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 1,
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
          stopReason, timestamp: Date.now(),
        };
        if (stopReason === "stop" || stopReason === "toolUse") {
          stream.push({ type: "done", reason: stopReason, message: result });
        }
        else stream.push({ type: "error", reason: stopReason,
          error: { ...result, errorMessage: "Synthetic " + stopReason } });
        stream.end();
      }
      if (text === "hold for interrupt") {
        if (options.signal.aborted) finish("aborted");
        else options.signal.addEventListener("abort", () => finish("aborted"), { once: true });
      } else if (text === "read scoped transcript") {
        const start = context.messages.findLastIndex((message) => message.role === "user");
        const reads = context.messages.slice(start + 1).filter((message) =>
          message.role === "toolResult" && message.toolName === "kinaiya_read_transcript");
        const last = reads.at(-1);
        if (last?.isError) {
          finish("stop", last.content);
          return stream;
        }
        const pages = reads.map((read) => JSON.parse(read.content[0].text));
        const page = pages.at(-1);
        if (page && !page.has_more) {
          const entries = pages.flatMap((item) => item.entries);
          finish("stop", [{ type: "text", text: JSON.stringify({ entries, has_more: false }) }]);
          return stream;
        }
        const position = page ? { ...page.next_after, through_sequence: page.through_sequence } : {};
        finish("toolUse", [{
          type: "toolCall", id: "fixture_transcript_" + reads.length,
          name: "kinaiya_read_transcript", arguments: { ...position, limit: 1 },
        }]);
      } else {
        finish(text === "synthetic provider failure" ? "error" : "stop");
      }
      return stream;
    },
  });
}
