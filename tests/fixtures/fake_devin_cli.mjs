import readline from "node:readline";

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", (line) => {
  const request = JSON.parse(line);
  let result = {};
  if (request.method === "authenticate") {
    if (request.params?._meta?.api_key !== "synthetic-session-token") {
      process.stdout.write(JSON.stringify({
        jsonrpc: "2.0", id: request.id, error: { code: -1, message: "auth failed" },
      }) + "\n");
      return;
    }
  } else if (request.method === "session/new") {
    result = {
      sessionId: "synthetic-session",
      configOptions: [{
        id: "model", currentValue: "fake-model", options: [{ value: "fake-model" }],
      }],
    };
  } else if (request.method === "session/prompt") {
    const text = JSON.stringify({ scores: [50, 75] });
    process.stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId: "synthetic-session",
        update: { sessionUpdate: "agent_message_chunk", content: { type: "text", text } },
      },
    }) + "\n");
    process.stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      method: "_cognition.ai/agent_stopped",
      params: { stats: { modelLabel: "fake-model", creditCost: 0 } },
    }) + "\n");
  } else if (request.method === "session/set_config_option") {
    result = { configOptions: [{ id: "model", currentValue: request.params.value }] };
  }
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: request.id, result }) + "\n");
});
