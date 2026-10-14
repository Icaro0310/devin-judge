import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  ABSTAIN,
  DecisionClient,
  doDecide,
  doGate,
  doJudge,
  judgeTool,
  classifyTool,
  rateTool,
  parseLevels,
  noulVerdict,
  distVerdict,
  DEFAULT_GATE_CHECKS,
  GATE_RISK_LEVELS,
  SELF_ASSESSMENT_HYPOTHESIS,
} from '../src/decisions.ts';
import { applyTemperature } from '../src/calibration.ts';
import type { Pair } from '../src/scoring.ts';

/** Backend scripted per-pair by hypothesis content; records every batch. */
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

test('judgeTool: noul pair = (premise, statement), no template', async () => {
  const backend = scripted(() => 0.8);
  const client = new DecisionClient(backend);
  const r = await judgeTool(
    client,
    'the fix resolves the bug',
    'the deploy log is clean',
  );
  assert.deepEqual(backend.calls, [
    [['the deploy log is clean', 'the fix resolves the bug']],
  ]);
  assert.equal(r.answer, 'yes');
  assert.equal(r.p_yes, 0.8);
  assert.equal(r.p_no, 0.2);
  assert.equal(r.confidence, 0.8);
  assert.equal(r.confidence_source, 'self_report');
  assert.equal(r.low_confidence, false);
  assert.equal(r.verdict, 'yes');
  assert.equal(r.uncalibrated, false);
  assert.equal(r.basis, 'premise');
});

test('judgeTool: no context asks self-assessment, flagged uncalibrated', async () => {
  const backend = scripted(() => 0.8);
  const client = new DecisionClient(backend);
  const r = await judgeTool(client, 'the sky is blue');
  // The pair is the statement's own self-assessment — the only well-formed
  // entailment question without a premise — never (instruction, claim).
  assert.deepEqual(backend.calls, [
    [['the sky is blue', SELF_ASSESSMENT_HYPOTHESIS]],
  ]);
  assert.equal(r.uncalibrated, true);
  assert.equal(r.basis, 'self-assessment only');
  assert.equal(r.confidence_source, 'self_report');
  // Whitespace-only context counts as no context.
  const blank = scripted(() => 0.8);
  await judgeTool(new DecisionClient(blank), 's', '   ');
  assert.deepEqual(blank.calls, [[['s', SELF_ASSESSMENT_HYPOTHESIS]]]);
});

test('judgeTool: low probability flips answer and verdict', async () => {
  const client = new DecisionClient(scripted(() => 0.1));
  const r = await judgeTool(client, 's');
  assert.equal(r.answer, 'no');
  assert.equal(r.confidence, 0.9); // confidence = 1 - p when value=false
  assert.equal(r.verdict, 'strong_no');
});

test('judgeTool: review band + low_confidence forces review', async () => {
  const client = new DecisionClient(scripted(() => 0.5));
  const r = await judgeTool(client, 's');
  assert.equal(r.verdict, 'review');
  // confidence 0.5 < 0.6 -> low_confidence even outside the review band
  const edge = new DecisionClient(scripted(() => 0.9));
  const r2 = await judgeTool(edge, 's', '', 0.95);
  assert.equal(r2.low_confidence, true);
  assert.equal(r2.verdict, 'review');
});

test('classifyTool: one pair per option with the hypothesis template', async () => {
  const backend = scripted(([, h]) =>
    h === 'This example is billing.' ? 0.7 : 0.15);
  const client = new DecisionClient(backend);
  const r = await classifyTool(client, 'my invoice is wrong', [
    'billing', 'technical', 'other',
  ]);
  assert.equal(backend.calls.length, 1);
  assert.deepEqual(
    backend.calls[0]?.map(([, h]) => h),
    ['This example is billing.', 'This example is technical.', 'This example is other.'],
  );
  assert.equal(r.choice, 'billing');
  // 0.7/0.15/0.15 normalized
  assert.ok(Math.abs((r.distribution['billing'] ?? 0) - 0.7) < 1e-4);
  assert.equal(r.confidence_source, 'self_report');
});

test('rateTool: numeric range scale + expected-index score', async () => {
  const backend = scripted(([, h]) =>
    h === 'This example is 5.' ? 0.6 : h === 'This example is 4.' ? 0.3 : 0.1 / 3);
  const client = new DecisionClient(backend);
  const r = await rateTool(client, 'the meal', '1-5');
  assert.equal(r.level, '5');
  assert.ok(Math.abs(r.confidence - 0.6) < 1e-4);
  // score = expected 0-based level index
  assert.ok(r.score > 3);
  assert.equal(Object.keys(r.distribution).length, 5);
});

test('rateTool: named levels list', async () => {
  const backend = scripted(([, h]) => (h.includes('high') ? 0.8 : 0.1));
  const client = new DecisionClient(backend);
  const r = await rateTool(client, 'risk', ['low', 'medium', 'high']);
  assert.equal(r.level, 'high');
});

test('doGate: risk classify + both named checks batched in one call', async () => {
  const backend = scripted(([, h]) => {
    if (h === 'This example is low.') return 0.7;
    if (h === DEFAULT_GATE_CHECKS['moves_money']) return 0.9;
    if (h === DEFAULT_GATE_CHECKS['deletes_data']) return 0.05;
    return 0.1;
  });
  const client = new DecisionClient(backend);
  const r = await doGate(client, 'refund order 123');
  assert.equal(backend.calls.length, 2); // 3-option classify, then 2 nouls together
  assert.equal(backend.calls[0]?.length, GATE_RISK_LEVELS.length);
  assert.equal(backend.calls[1]?.length, 2);
  assert.equal(r.risk, 'low');
  assert.equal(r.advisory, true);
  assert.equal(r.details['moves_money'], 0.9);
  assert.equal(r.details['deletes_data'], 0.05);
  assert.equal(r.low_confidence, false);
  assert.equal(r.verdict, 'confident');
});

test('doDecide: mixed primitive specs batch into ONE backend call', async () => {
  const backend = scripted(([, h]) => {
    if (h.includes('red')) return 0.8;
    if (h.includes('a-level')) return 0.6;
    if (h === 'It works.') return 0.9;
    return 0.2;
  });
  const client = new DecisionClient(backend);
  const out = await doDecide(client, 'the deploy', {
    color: { type: 'choice', options: ['red', 'blue'] },
    lvl: { type: 'score', levels: ['a-level', 'b-level'] },
    ok: { type: 'noul', statement: 'It works.' },
  });
  assert.equal(backend.calls.length, 1);
  assert.equal(backend.calls[0]?.length, 5); // 2 + 2 + 1 pairs
  assert.equal(out['color']?.value, 'red');
  assert.ok(out['color']?.distribution);
  assert.equal(out['lvl']?.value, 'a-level');
  assert.ok(out['lvl']?.score !== undefined);
  assert.equal(out['ok']?.value, true);
  assert.equal(out['ok']?.prob_true, 0.9);
});

test('temperature rescales confidences, never the winner (noul)', async () => {
  const client = new DecisionClient(scripted(() => 0.9), { temperature: 2 });
  const r = await judgeTool(client, 's');
  // softmax(log p / 2) on [0.1, 0.9] is exactly [0.25, 0.75]
  assert.ok(Math.abs(r.p_yes - 0.75) < 1e-4, `p_yes=${r.p_yes}`);
  assert.equal(r.answer, 'yes');
  assert.equal(r.confidence, 0.75);
});

test('temperature rescales confidences, never the winner (choice)', async () => {
  const client = new DecisionClient(
    scripted(([, h]) => (h.includes('alpha') ? 0.8 : 0.2)),
    { temperature: 2 },
  );
  const r = await classifyTool(client, 'x', ['alpha', 'beta']);
  assert.equal(r.choice, 'alpha');
  // sqrt(0.2/0.8) = 0.5 -> odds 2:1 -> 0.6667
  assert.ok(Math.abs(r.confidence - 2 / 3) < 1e-3);
});

test('abstain threshold marks value ABSTAIN', async () => {
  const client = new DecisionClient(scripted(() => 0.55), {
    abstainThreshold: 0.9,
  });
  const r = await doJudge(client, 'p', 's');
  assert.equal(r.abstained, true);
  assert.equal(r.value, ABSTAIN);
});

test('parseLevels mirrors _levels: int, range, dots, list, errors', () => {
  assert.deepEqual(parseLevels(4), ['1', '2', '3', '4']);
  assert.deepEqual(parseLevels('1-5'), ['1', '2', '3', '4', '5']);
  assert.deepEqual(parseLevels('0..2'), ['0', '1', '2']);
  assert.deepEqual(parseLevels(['a', 'b']), ['a', 'b']);
  assert.throws(() => parseLevels('nope'), /cannot parse scale/);
  assert.throws(() => parseLevels('5-1'), /cannot parse scale/);
  assert.throws(() => parseLevels('1-99'), /cannot parse scale/); // span > 20
  assert.throws(() => parseLevels([]), /must not be empty/);
  assert.throws(() => parseLevels(true), /must be a list, int, or range/);
});

test('verdict bands match verdicts.py', () => {
  assert.equal(noulVerdict(0.1), 'strong_no');
  assert.equal(noulVerdict(0.3), 'no');
  assert.equal(noulVerdict(0.5), 'review');
  assert.equal(noulVerdict(0.7), 'yes');
  assert.equal(noulVerdict(0.9), 'strong_yes');
  assert.equal(noulVerdict(0.9, true), 'review');
  assert.equal(distVerdict(false), 'confident');
  assert.equal(distVerdict(true), 'review');
});

test('applyTemperature port: T=1 no-op, T<=0 throws', () => {
  const d = [0.7, 0.2, 0.1];
  const same = applyTemperature(d, 1);
  assert.ok(same.every((v, i) => Math.abs(v - (d[i] ?? 0)) < 1e-9));
  assert.throws(() => applyTemperature(d, 0), /> 0/);
  // T -> inf approaches uniform
  const flat = applyTemperature(d, 1e6);
  assert.ok(Math.abs((flat[0] ?? 0) - 1 / 3) < 1e-3);
});
