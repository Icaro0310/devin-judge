/**
 * `@judge` chat answers — pure markdown model, no vscode imports.
 * Parses the slash-command prompt syntaxes, runs the decision layer
 * (src/decisions.ts) over a caller-supplied entail function, and renders
 * honest, compact markdown. Errors become honest messages — the chat
 * surface never propagates a raw throw.
 */

import {
  doDecide,
  doGate,
  judgeTool,
  classifyTool,
  rateTool,
} from '../../src/decisions.ts';
import type {
  DecisionClient,
  DecideEntry,
  QuestionSpec,
} from '../../src/decisions.ts';

const USAGE = [
  '**judge**: calibrated decisions on the model VS Code is serving:',
  '- `/judge <context> | <statement>`: yes/no verdict on <statement> weighed against <context>',
  '- `/classify <text> | <option> | <option> | …`: pick exactly one option',
  '- `/rate <text> | 1-5` or `/rate <text> | low,medium,high`: rating on a scale',
  '- `/gate <action>`: advisory risk check (moves money / deletes data)',
  '- `/decide {"state":"…","questions":{"name":{"type":"noul|choice|score",…}}}`: batch typed questions',
  '',
  '_advisory only: a verdict to weigh, never a command_',
].join('\n');

export interface ChatContext {
  temperature?: number;
}

function fmtDist(dist: Record<string, number>): string {
  return Object.entries(dist)
    .map(([k, v]) => `\`${k}\` ${(v * 100).toFixed(1)}%`)
    .join(' · ');
}

function fmtFooter(temperature: number | undefined, extra?: string): string {
  const bits = ['confidence_source: self_report'];
  if (temperature !== undefined && temperature !== 1) {
    bits.push(`calibrated T=${temperature}`);
  }
  if (extra) bits.push(extra);
  return `\n\n_${bits.join(' · ')}_`;
}

function fail(message: string): string {
  return `**judge**: ${message}`;
}

async function runJudge(
  client: DecisionClient,
  prompt: string,
  ctx: ChatContext,
): Promise<string> {
  if (!prompt.trim()) return USAGE;
  // No premise: the only pair left is instruction-vs-claim, which is not a
  // well-formed entailment question — the model's number would be noise
  // rendered as a verdict. The chat surface abstains instead of calling the
  // model at all (the judge *tool* marks the same call uncalibrated).
  // The separator is the SPACED pipe " | " and must appear exactly once:
  // unspaced 'a|b' is content, and multiple separators are ambiguous —
  // picking first-vs-last would silently judge the wrong claim.
  const parts = prompt.split(' | ');
  if (parts.length > 2) {
    return [
      '**ABSTAIN** · ambiguous — ` | ` appears more than once',
      '`/judge <context> | <statement>` — exactly one spaced pipe separates evidence from the claim',
    ].join('\n');
  }
  const context = parts.length === 2 ? (parts[0] ?? '').trim() : '';
  const statement = parts.length === 2 ? (parts[1] ?? '').trim() : '';
  if (!context || !statement) {
    return [
      '**ABSTAIN** · calibrated judgement requires a premise',
      '`/judge <context> | <statement>` — the context is the evidence the statement is weighed against',
    ].join('\n');
  }
  const r = await judgeTool(client, statement, context);
  const lines = [
    `**${r.answer.toUpperCase()}** · ${r.verdict}`,
    `p_yes ${r.p_yes} · p_no ${r.p_no} · confidence ${r.confidence}` +
      (r.low_confidence ? ' · **low_confidence**' : ''),
  ];
  return lines.join('\n') + fmtFooter(ctx.temperature);
}

async function runClassify(
  client: DecisionClient,
  prompt: string,
  ctx: ChatContext,
): Promise<string> {
  const parts = prompt.split('|').map((s) => s.trim());
  const text = parts[0] ?? '';
  const options = parts.slice(1).filter((s) => s !== '');
  if (!text || options.length < 2) {
    return fail(
      'classify needs `text | option | option | …` (at least two options)',
    );
  }
  const r = await classifyTool(client, text, options);
  return (
    [
      `**${r.choice}** · ${r.verdict}`,
      fmtDist(r.distribution),
      `confidence ${r.confidence}${r.low_confidence ? ' · **low_confidence**' : ''}`,
    ].join('\n') + fmtFooter(ctx.temperature)
  );
}

async function runRate(
  client: DecisionClient,
  prompt: string,
  ctx: ChatContext,
): Promise<string> {
  const parts = prompt.split('|').map((s) => s.trim());
  const text = parts[0] ?? '';
  const scaleRaw = parts.slice(1).join(' ').trim();
  if (!text || !scaleRaw) {
    return fail('rate needs `text | 1-5` or `text | level1,level2,…`');
  }
  const scale: unknown = scaleRaw.includes(',')
    ? scaleRaw.split(',').map((s) => s.trim()).filter((s) => s !== '')
    : scaleRaw;
  const r = await rateTool(client, text, scale);
  return (
    [
      `**${r.level}** (score ${r.score}) · ${r.verdict}`,
      fmtDist(r.distribution),
      `confidence ${r.confidence}${r.low_confidence ? ' · **low_confidence**' : ''}`,
    ].join('\n') + fmtFooter(ctx.temperature)
  );
}

async function runGate(
  client: DecisionClient,
  prompt: string,
  ctx: ChatContext,
): Promise<string> {
  if (!prompt.trim()) return fail('gate needs an action: `/gate <action>`');
  const r = await doGate(client, prompt);
  const lines = [
    `**risk: ${r.risk}** · ${r.verdict} (advisory)`,
    fmtDist(r.distribution),
    `confidence ${r.confidence}${r.low_confidence ? ' · **low_confidence**' : ''}`,
    'checks:',
  ];
  for (const [name, v] of Object.entries(r.details)) {
    lines.push(
      `- \`${name}\`: ${typeof v === 'number' ? `p_yes ${v}` : `error: ${v.error}`}`,
    );
  }
  return lines.join('\n') + fmtFooter(ctx.temperature);
}

async function runDecide(
  client: DecisionClient,
  prompt: string,
  ctx: ChatContext,
): Promise<string> {
  let spec: { state?: unknown; questions?: unknown };
  try {
    spec = JSON.parse(prompt) as { state?: unknown; questions?: unknown };
  } catch {
    return fail(
      'decide needs JSON: `{"state":"…","questions":{"name":{"type":"noul|choice|score",…}}}`',
    );
  }
  if (typeof spec.state !== 'string' || spec.state === '') {
    return fail('decide needs a non-empty "state" string');
  }
  if (
    typeof spec.questions !== 'object' ||
    spec.questions === null ||
    Array.isArray(spec.questions) ||
    Object.keys(spec.questions).length === 0
  ) {
    return fail('decide needs a "questions" object: {name: {type: …}}');
  }
  const answers = await doDecide(
    client,
    spec.state,
    spec.questions as Record<string, QuestionSpec>,
  );
  const lines = ['**decide**'];
  for (const [name, e] of Object.entries(answers)) {
    lines.push(formatEntry(name, e));
  }
  return lines.join('\n') + fmtFooter(ctx.temperature);
}

function formatEntry(name: string, e: DecideEntry): string {
  const head =
    `- \`${name}\`: **${String(e.value)}** (confidence ${e.confidence}` +
    `${e.abstained ? ', abstained' : ''})`;
  const parts: string[] = [];
  if (e.prob_true !== undefined) parts.push(`p_yes ${e.prob_true}`);
  if (e.score !== undefined) parts.push(`score ${e.score}`);
  if (e.distribution !== undefined) parts.push(fmtDist(e.distribution));
  return parts.length ? `${head} · ${parts.join(' · ')}` : head;
}

/** Dispatch one participant request. Never throws: failures render as an
 *  honest markdown message. */
export async function handleChat(
  command: string | undefined,
  prompt: string,
  client: DecisionClient,
  ctx: ChatContext = {},
): Promise<string> {
  try {
    switch (command) {
      case undefined:
      case 'judge':
        return await runJudge(client, prompt, ctx);
      case 'classify':
        return await runClassify(client, prompt, ctx);
      case 'rate':
        return await runRate(client, prompt, ctx);
      case 'gate':
        return await runGate(client, prompt, ctx);
      case 'decide':
        return await runDecide(client, prompt, ctx);
      default:
        return USAGE;
    }
  } catch (error) {
    return fail(error instanceof Error ? error.message : String(error));
  }
}
