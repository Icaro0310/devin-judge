import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServerCore } from '../src/serverCore.ts';
import type { HttpRequestLike } from '../src/serverCore.ts';
import { NoModelError } from '../src/scoring.ts';
import type { Pair } from '../src/scoring.ts';

const TOKEN = 'test-token';

function req(over: Partial<HttpRequestLike> = {}): HttpRequestLike {
  return {
    method: 'POST',
    path: '/score',
    headers: { authorization: `Bearer ${TOKEN}` },
    body: '{}',
    ...over,
  };
}

function deps(over: Partial<Parameters<typeof createServerCore>[0]> = {}) {
  const calls: Pair[][] = [];
  return {
    calls,
    deps: {
      token: TOKEN,
      getModel: () => 'copilot/fake',
      async scorePairs(pairs: Pair[]) {
        calls.push(pairs);
        return { probs: pairs.map(() => 42), model: 'copilot/fake' };
      },
      ...over,
    },
  };
}

test('wrong path -> 404, wrong method -> 405', async () => {
  const { deps: d } = deps();
  const core = createServerCore(d);
  assert.equal((await core.handleRequest(req({ path: '/other' }))).status, 404);
  assert.equal((await core.handleRequest(req({ method: 'GET' }))).status, 405);
});

test('missing or wrong bearer token -> 401', async () => {
  const { deps: d } = deps();
  const core = createServerCore(d);
  assert.equal((await core.handleRequest(req({ headers: {} }))).status, 401);
  assert.equal(
    (
      await core.handleRequest(
        req({ headers: { authorization: 'Bearer nope' } }),
      )
    ).status,
    401,
  );
});

test('bad JSON and non-object payloads -> 400', async () => {
  const { deps: d } = deps();
  const core = createServerCore(d);
  assert.equal((await core.handleRequest(req({ body: 'not json' }))).status, 400);
  assert.equal((await core.handleRequest(req({ body: '[1,2]' }))).status, 400);
  assert.equal((await core.handleRequest(req({ body: '"x"' }))).status, 400);
});

test('warmup -> 200 with cached model (null before first score)', async () => {
  const { deps: d } = deps({ getModel: () => null });
  const core = createServerCore(d);
  const out = await core.handleRequest(req({ body: '{"warmup": true}' }));
  assert.equal(out.status, 200);
  assert.deepEqual(out.json, { warmup: true, model: null });
});

test('invalid pairs shape -> 400 (client error, not a model failure)', async () => {
  const { deps: d } = deps();
  const core = createServerCore(d);
  for (const body of [
    '{"pairs": "x"}',
    '{"pairs": [["only-one"]]}',
    '{"pairs": [["p", 3]]}',
    '{}',
  ]) {
    const out = await core.handleRequest(req({ body }));
    assert.equal(out.status, 400, body);
    assert.ok('error' in out.json);
  }
});

test('valid pairs -> 200 {"probs", "model"}', async () => {
  const { deps: d, calls } = deps();
  const core = createServerCore(d);
  const out = await core.handleRequest(
    req({ body: JSON.stringify({ pairs: [['p1', 'h1'], ['p2', 'h2']] }) }),
  );
  assert.equal(out.status, 200);
  assert.deepEqual(out.json, { probs: [42, 42], model: 'copilot/fake' });
  assert.equal(calls.length, 1);
});

test('NoModelError -> 503 (the only fallback-eligible signal)', async () => {
  const { deps: d } = deps({
    async scorePairs() {
      throw new NoModelError('no chat model selected');
    },
  });
  const core = createServerCore(d);
  const out = await core.handleRequest(
    req({ body: '{"pairs": [["p", "h"]]}' }),
  );
  assert.equal(out.status, 503);
  assert.match(String(out.json['error']), /no chat model/);
});

test('any other scoring failure -> 200 {"error"} (protocol error upstream)', async () => {
  const { deps: d } = deps({
    async scorePairs() {
      throw new Error('model returned scores outside the expected 0-100 range');
    },
  });
  const core = createServerCore(d);
  const out = await core.handleRequest(
    req({ body: '{"pairs": [["p", "h"]]}' }),
  );
  assert.equal(out.status, 200);
  assert.match(String(out.json['error']), /0-100/);
});

test('requests serialize: one scorePairs call at a time', async () => {
  let inFlight = 0;
  let maxInFlight = 0;
  const order: string[] = [];
  const { deps: d } = deps({
    async scorePairs(pairs: Pair[]) {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await new Promise((r) => setTimeout(r, 15));
      inFlight -= 1;
      order.push(String(pairs[0]?.[0]));
      return { probs: [1], model: 'm' };
    },
  });
  const core = createServerCore(d);
  const results = await Promise.all([
    core.handleRequest(req({ body: '{"pairs": [["first", "h"]]}' })),
    core.handleRequest(req({ body: '{"pairs": [["second", "h"]]}' })),
    core.handleRequest(req({ body: '{"pairs": [["third", "h"]]}' })),
  ]);
  assert.equal(maxInFlight, 1, 'never two model calls at once');
  assert.deepEqual(order, ['first', 'second', 'third']);
  assert.ok(results.every((r) => r.status === 200));
});
