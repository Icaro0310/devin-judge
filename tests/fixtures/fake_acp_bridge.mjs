/**
 * fake_acp_bridge.mjs — stand-in determinístico para djaevin-acp-bridge.mjs.
 * Sem ACP, sem modelo: responde probs fixas para o protocolo poder ser
 * testado offline. pairs[i][1] (hypothesis) contendo "error" -> erro
 * estruturado, para exercitar o caminho de falha.
 */

import readline from "node:readline";

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  let req;
  try { req = JSON.parse(line); } catch { return; }
  if (req.warmup) {
    process.stdout.write(JSON.stringify({ id: req.id, warmup: true, model: "fake" }) + "\n");
    return;
  }
  const pairs = req.pairs || [];
  if (pairs.some((p) => /error/i.test(p[1]))) {
    process.stdout.write(JSON.stringify({ id: req.id, error: "fake bridge error" }) + "\n");
    return;
  }
  const scores = pairs.map((_, i) => 50 + i); // determinístico: 50, 51, 52...
  // Marcadores sintéticos: "unknown" omite telemetry; "paid" reports cost 5;
  // other turns explicitly report numeric zero.
  const unknown = pairs.some((p) => /unknown/i.test(p[1]));
  const paid = pairs.some((p) => /paid/i.test(p[1]));
  const response = { id: req.id, probs: scores, model: "fake" };
  if (!unknown) response.cost = paid ? 5 : 0;
  process.stdout.write(JSON.stringify(response) + "\n");
});
