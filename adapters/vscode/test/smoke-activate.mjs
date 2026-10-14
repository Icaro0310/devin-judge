// Activation smoke test — runs the COMPILED extension entry against the
// vscode stub, then exercises the REAL http endpoint it started (node:http
// needs no extension host) plus the registered @judge handler.
// Prereq: npm run build. Usage: node --import ./test/vscode-loader-import.mjs test/smoke-activate.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// Redirect the endpoint discovery file into a scratch data dir BEFORE
// activate() runs (endpointFilePath reads XDG_DATA_HOME at request time).
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'judge-smoke-'));
process.env.XDG_DATA_HOME = tmp;

const vscode = await import('vscode');
const ext = await import('../out/adapters/vscode/extension.js');

const subscriptions = [];
await ext.activate({
  subscriptions,
  extension: { packageJSON: { version: '0.0.0-smoke' } },
});
const r = vscode.registered;

assert.deepEqual(
  r.participants.map((p) => p.id),
  ['judge.judge'],
  'participant id matches manifest',
);
for (const c of ['judge', 'classify', 'rate', 'gate', 'decide']) {
  assert.ok(
    r.commands.some((x) => x.id === `judge.${c}`),
    `command judge.${c} registered`,
  );
}
assert.ok(r.outputs.some((o) => o.name === 'Judge'), 'output channel created');
assert.equal(r.selectCalls.length, 0, 'no selectChatModels at activate (consent needs a gesture)');

// --- the real endpoint: discovery file -> auth -> warmup -> 503 -> score ---
const file = path.join(tmp, 'poordjaevin', 'vscode-lm-endpoint.json');
assert.ok(fs.existsSync(file), 'endpoint file advertised after listen');
const spec = JSON.parse(fs.readFileSync(file, 'utf8'));
assert.match(spec.url, /^http:\/\/127\.0\.0\.1:\d+\/score$/);
assert.equal(spec.token.length, 48);
assert.equal(spec.pid, process.pid);
assert.equal(spec.extension, 'icaro0310.judge');
if (process.platform !== 'win32') {
  assert.equal(fs.statSync(file).mode & 0o777, 0o600, 'endpoint file is 0600');
}

const post = async (payload, token = spec.token) => {
  const resp = await fetch(spec.url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(payload),
  });
  return { status: resp.status, json: await resp.json() };
};

assert.equal((await post({ warmup: true }, 'wrong')).status, 401, 'bad token rejected');
let out = await post({ warmup: true });
assert.deepEqual(out, { status: 200, json: { warmup: true, model: null } }, 'warmup, no model yet');

// no chat models registered -> 503 (fallback-eligible upstream)
out = await post({ pairs: [['p', 'h']] });
assert.equal(out.status, 503);
assert.match(out.json.error, /no chat model/);

// register a fake model -> the endpoint scores through it
vscode.registered.models = [
  vscode.fakeChatModel((messages) => {
    assert.equal(messages.length, 1, 'one user message');
    assert.match(messages[0].content, /estimate from 0 to 100/);
    const pairs = JSON.parse(messages[0].content.split('\n\n').at(-1));
    return `junk {"scores":${JSON.stringify(pairs.map((_, i) => 40 + i * 10))}} tail`;
  }),
];
out = await post({ pairs: [['p1', 'h1'], ['p2', 'h2'], ['p3', 'h3']] });
assert.equal(out.status, 200);
assert.deepEqual(out.json.probs, [40, 50, 60]);
assert.equal(out.json.model, 'copilot/fake');
out = await post({ warmup: true });
assert.deepEqual(out.json, { warmup: true, model: 'copilot/fake' }, 'warmup reports last model');

// --- @judge participant over the same fake model ---
const chunks = [];
const stream = { markdown(m) { chunks.push(String(m)); } };
await r.participants[0].handler(
  { prompt: 'the fix resolves the bug', command: 'judge' },
  {},
  stream,
  {},
);
// fake model scores the single noul pair 40 -> p_yes 0.4 -> NO
assert.match(chunks.join(''), /\*\*NO\*\*/, 'judge renders a verdict');
assert.match(chunks.join(''), /p_yes 0\.4/, 'judge renders p_yes');

// commands prefill the chat input (workbench.action.chat.open recorded)
await r.commands.find((c) => c.id === 'judge.gate').fn();
const opened = r.executedCommands.find((c) => c.id === 'workbench.action.chat.open');
assert.equal(opened.args[0].query, '@judge /gate ');
assert.equal(opened.args[0].isPartialQuery, true);

// --- deactivate removes the discovery file and stops listening ---
await ext.deactivate();
assert.equal(fs.existsSync(file), false, 'endpoint file removed on deactivate');
await assert.rejects(post({ warmup: true }), /ECONNREFUSED|fetch failed/, 'server closed');

fs.rmSync(tmp, { recursive: true, force: true });
console.log('smoke-activate: OK — participant, commands, endpoint lifecycle, real http round-trip');
