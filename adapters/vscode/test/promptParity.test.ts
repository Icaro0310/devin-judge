/**
 * Prompt parity tripwire. The entailment prompt lives in two places:
 *
 *   - `src/poordjaevin/scripts/djaevin-acp-bridge.mjs` — the ACP backend's
 *     production prompt (source of truth).
 *   - `adapters/vscode/src/scoring.ts` — the byte-for-byte port served by
 *     the vscode-lm extension.
 *
 * If they diverge, `acp` and `vscode-lm` stop measuring the same thing and
 * the calibration story breaks silently. This test reads both files and
 * fails when the instruction lines or the pair-serialization differ.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const repoRoot = path.resolve(import.meta.dirname, '..', '..', '..');
const bridgeSrc = fs.readFileSync(
  path.join(repoRoot, 'src/poordjaevin/scripts/djaevin-acp-bridge.mjs'),
  'utf8',
);
const scoringSrc = fs.readFileSync(
  path.join(repoRoot, 'adapters/vscode/src/scoring.ts'),
  'utf8',
);

/** Extract the instruction strings of the scoring prompt: the literals
 *  from "For every [premise" through "same order as the input.", in
 *  source order, unescaped (`\"` -> `"`). */
function instructionLiterals(src: string): string[] {
  const start = src.indexOf('For every [premise');
  const end = src.indexOf('same order as the input.');
  assert.ok(start >= 0 && end > start, 'instruction block not found');
  // Rewind to the opening quote of the first literal so the slice is exact.
  const q0 = src.slice(0, start).match(/["'](?:[^"'\\]|\\.)*$/);
  const q1 = src.slice(end + 'same order as the input.'.length).match(/^["']/);
  const block = src.slice(
    start - (q0 ? q0[0].length : 0) - 1,
    end + 'same order as the input.'.length + (q1 ? 1 : 0),
  );
  const literals = [...block.matchAll(/"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'/g)]
    .map((m) => (m[1] ?? m[2] ?? '').replace(/\\"/g, '"'));
  return literals;
}

test('extension scoring prompt matches the ACP bridge prompt', () => {
  const bridge = instructionLiterals(bridgeSrc);
  const scoring = instructionLiterals(scoringSrc);
  assert.equal(bridge.length, 3, 'bridge prompt has 3 instruction lines');
  assert.deepEqual(scoring, bridge);
});

test('both serialize pairs as the last prompt line via JSON.stringify', () => {
  for (const [name, src] of [
    ['bridge', bridgeSrc],
    ['scoring', scoringSrc],
  ] as const) {
    assert.match(
      src,
      /JSON\.stringify\(pairs\)/,
      `${name} appends JSON.stringify(pairs)`,
    );
  }
});
