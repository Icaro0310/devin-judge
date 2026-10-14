/**
 * @judge chat-surface semantics. The split that matters: without a
 * `context | statement` premise the surface abstains WITHOUT touching the
 * model — a (instruction, claim) pair is not a well-formed entailment
 * question, so there is nothing honest to ask.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { handleChat } from '../adapters/vscode/chatModel.ts';
import { DecisionClient } from '../src/decisions.ts';
import type { Pair } from '../src/scoring.ts';

function scripted(score: (pair: Pair) => number) {
  const calls: Pair[][] = [];
  return {
    calls,
    async entailProbs(pairs: Pair[]): Promise<number[]> {
      calls.push(pairs);
      return pairs.map(score);
    },
  };
}

test('chat /judge without premise abstains and never calls the model', async () => {
  const backend = scripted(() => 0.9);
  const client = new DecisionClient(backend);
  const md = await handleChat('judge', 'the sky is blue', client);
  assert.match(md, /ABSTAIN/);
  assert.match(md, /requires a premise/);
  assert.match(md, /\/judge <context> \| <statement>/);
  assert.equal(backend.calls.length, 0);
});

test('chat /judge bare command (no slash) also abstains', async () => {
  const backend = scripted(() => 0.9);
  const md = await handleChat(undefined, 'the sky is blue', new DecisionClient(backend));
  assert.match(md, /ABSTAIN/);
  assert.equal(backend.calls.length, 0);
});

test('chat /judge with `context | statement` runs the weighed verdict', async () => {
  const backend = scripted(() => 0.9);
  const client = new DecisionClient(backend);
  const md = await handleChat('judge', 'the deploy log is clean | the fix resolves the bug', client);
  assert.match(md, /YES/);
  assert.match(md, /p_yes 0\.9/);
  assert.deepEqual(backend.calls, [
    [['the deploy log is clean', 'the fix resolves the bug']],
  ]);
});

test('chat /judge: pipes inside evidence stay in the premise', async () => {
  const backend = scripted(() => 0.9);
  const client = new DecisionClient(backend);
  // The evidence is paste-able text and may contain '|' itself — the
  // judged statement is always the final segment.
  const md = await handleChat(
    'judge', 'log says a|b failed | the fix resolves the bug', client);
  assert.match(md, /YES/);
  assert.deepEqual(backend.calls, [
    [['log says a|b failed', 'the fix resolves the bug']],
  ]);
});

test('chat /judge abstains on ambiguous input (more than one " | ")', async () => {
  const backend = scripted(() => 0.9);
  const client = new DecisionClient(backend);
  const md = await handleChat('judge', 'a | b | c', client);
  assert.match(md, /ABSTAIN/);
  assert.match(md, /ambiguous/);
  // Never guess a side: the model is not called at all.
  assert.deepEqual(backend.calls, []);
});

test('chat /judge rejects dangling separator', async () => {
  const backend = scripted(() => 0.9);
  const client = new DecisionClient(backend);
  for (const prompt of ['statement |', '| statement', ' | ']) {
    const md = await handleChat('judge', prompt, client);
    assert.match(md, /ABSTAIN/);
  }
  assert.equal(backend.calls.length, 0);
});
