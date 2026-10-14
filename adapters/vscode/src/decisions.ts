/**
 * Decision layer — pure TypeScript port of the poordjaevin decision
 * contract over self-reported probabilities:
 *
 *   primitives.py  Choice / Score / Noul and their typed answers
 *   client.py      one batched entail_probs call per ask(), temperature
 *                  applied before decide
 *   mcp_server.py  do_judge / do_classify / do_rate / do_gate / do_decide
 *                  plus the tool-level response shapes (verdict, verdict
 *                  tiers, low_confidence, confidence_source)
 *   verdicts.py    noul_verdict / dist_verdict bands
 *   evaluate.py    _build_primitive typed-spec parsing for `decide`
 *
 * The vscode-lm wire format is self-reported 0-100 ints/halves, so every
 * decide() runs in kind "probs"/"prob" — never "logits". Pure module: no
 * vscode, no http, no filesystem.
 */

import { applyTemperature } from './calibration.ts';
import type { Pair } from './scoring.ts';

// ---------------------------------------------------------------------------
// Constants (mirrors of mcp_server.py / client.py / verdicts.py)
// ---------------------------------------------------------------------------

export const DEFAULT_TEMPLATE = 'This example is {}.';
// Hypothesis used when judge is called with no premise at all: the only
// well-formed entailment question left is the statement's own
// self-assessment — (statement, "the statement is true."). Callers flag
// the result `uncalibrated` so it is never read as a measurement.
export const SELF_ASSESSMENT_HYPOTHESIS = 'the statement is true.';
export const DEFAULT_GATE_CHECKS: Record<string, string> = {
  moves_money: 'This action moves, sends, or refunds money.',
  deletes_data: 'This action deletes or destroys data.',
};
export const GATE_RISK_LEVELS = ['low', 'medium', 'high'] as const;
export const ABSTAIN = 'ABSTAIN';
export const LOW_CONFIDENCE_THRESHOLD = 0.6;
export const CONFIDENCE_SOURCE = 'self_report';

// ---------------------------------------------------------------------------
// Numeric helpers (port of primitives.py — numerically stable, adversary
// tolerant)
// ---------------------------------------------------------------------------

function sanitize(xs: readonly number[]): number[] {
  return xs.map((x) => {
    let v = Number(x);
    if (Number.isNaN(v)) v = 0.0;
    else if (!Number.isFinite(v)) v = v > 0 ? 1e30 : -1e30;
    return v;
  });
}

export function softmax(scores: readonly number[]): number[] {
  const xs = sanitize(scores);
  if (xs.length === 0) throw new Error('softmax requires at least one score');
  const m = Math.max(...xs);
  const exps = xs.map((x) => Math.exp(x - m));
  const total = exps.reduce((a, b) => a + b, 0);
  if (total <= 0 || Number.isNaN(total)) {
    return xs.map(() => 1 / xs.length);
  }
  return exps.map((e) => e / total);
}

export function normalizeProbs(scores: readonly number[]): number[] {
  let xs = sanitize(scores).map((x) => Math.max(0, x));
  let total = xs.reduce((a, b) => a + b, 0);
  if (!Number.isFinite(total) && xs.length > 0) {
    const m = Math.max(...xs);
    xs = xs.map((x) => x / m);
    total = xs.reduce((a, b) => a + b, 0);
  }
  if (total <= 0) {
    const n = xs.length > 0 ? xs.length : 1;
    return Array.from({ length: n }, () => 1 / n);
  }
  return xs.map((x) => x / total);
}

export function sigmoid(x: number): number {
  const v = sanitize([x])[0] ?? 0;
  if (v >= 0) {
    const z = Math.exp(-v);
    return 1 / (1 + z);
  }
  const z = Math.exp(v);
  return z / (1 + z);
}

type DistKind = 'logits' | 'probs';

function toDistribution(scores: readonly number[], kind: DistKind): number[] {
  if (kind === 'logits') return softmax(scores);
  if (kind === 'probs') return normalizeProbs(scores);
  throw new Error(`kind must be 'logits' or 'probs', got ${String(kind)}`);
}

function round4(x: number): number {
  return Math.round(x * 10000) / 10000;
}

// ---------------------------------------------------------------------------
// Typed answers + question primitives (port of primitives.py)
// ---------------------------------------------------------------------------

interface AnswerBase {
  readonly kind: 'choice' | 'score' | 'noul';
  readonly confidence: number;
  readonly abstained: boolean;
}

export interface ChoiceAnswer extends AnswerBase {
  readonly kind: 'choice';
  readonly value: string;
  readonly probs: Record<string, number>;
}

export interface ScoreAnswer extends AnswerBase {
  readonly kind: 'score';
  readonly value: string;
  readonly score: number;
  readonly distribution: Record<string, number>;
}

export interface NoulAnswer extends AnswerBase {
  readonly kind: 'noul';
  readonly value: boolean;
  readonly prob: number;
}

export type AnyAnswer = ChoiceAnswer | ScoreAnswer | NoulAnswer;
export type Primitive = Choice | Score | Noul;

export class Choice {
  readonly options: string[];

  constructor(options: readonly string[]) {
    if (!Array.isArray(options)) {
      throw new TypeError('Choice options must be a list of strings');
    }
    const opts = [...options];
    if (opts.length < 2) throw new Error('Choice needs at least 2 options');
    if (opts.some((o) => typeof o !== 'string' || o === '')) {
      throw new Error('Choice options must be non-empty strings');
    }
    if (new Set(opts).size !== opts.length) {
      throw new Error('Choice options must be unique');
    }
    this.options = opts;
  }

  get hypotheses(): string[] {
    return [...this.options];
  }

  decide(
    scores: readonly number[],
    kind: DistKind = 'logits',
    threshold = 0,
  ): ChoiceAnswer {
    const xs = [...scores];
    if (xs.length !== this.options.length) {
      throw new Error(
        `expected ${this.options.length} scores, got ${xs.length}`,
      );
    }
    const probs = toDistribution(xs, kind);
    let best = 0;
    probs.forEach((p, i) => {
      if (p > (probs[best] ?? 0)) best = i;
    });
    const value = this.options[best] ?? '';
    const confidence = probs[best] ?? 0;
    return {
      kind: 'choice',
      value,
      probs: Object.fromEntries(this.options.map((o, i) => [o, probs[i] ?? 0])),
      confidence,
      abstained: confidence < threshold,
    };
  }
}

export class Score {
  readonly levels: string[];

  constructor(levels: readonly string[]) {
    if (!Array.isArray(levels)) {
      throw new TypeError('Score levels must be a list of strings');
    }
    const lv = [...levels];
    if (lv.length < 2) throw new Error('Score needs at least 2 ordered levels');
    if (lv.some((x) => typeof x !== 'string' || x === '')) {
      throw new Error('Score levels must be non-empty strings');
    }
    if (new Set(lv).size !== lv.length) {
      throw new Error('Score levels must be unique');
    }
    this.levels = lv;
  }

  get hypotheses(): string[] {
    return [...this.levels];
  }

  decide(
    scores: readonly number[],
    kind: DistKind = 'logits',
    threshold = 0,
  ): ScoreAnswer {
    const xs = [...scores];
    if (xs.length !== this.levels.length) {
      throw new Error(
        `expected ${this.levels.length} scores, got ${xs.length}`,
      );
    }
    const probs = toDistribution(xs, kind);
    const expected = probs.reduce((sum, p, i) => sum + i * p, 0);
    let best = 0;
    probs.forEach((p, i) => {
      if (p > (probs[best] ?? 0)) best = i;
    });
    const value = this.levels[best] ?? '';
    const confidence = probs[best] ?? 0;
    return {
      kind: 'score',
      value,
      score: expected,
      distribution: Object.fromEntries(
        this.levels.map((lv, i) => [lv, probs[i] ?? 0]),
      ),
      confidence,
      abstained: confidence < threshold,
    };
  }
}

export class Noul {
  readonly statement: string;

  constructor(statement: string) {
    if (typeof statement !== 'string' || statement.trim() === '') {
      throw new Error('Noul needs a non-empty statement');
    }
    this.statement = statement;
  }

  get hypotheses(): string[] {
    return [this.statement];
  }

  decide(
    score: number | readonly number[],
    kind: 'logit' | 'prob' = 'logit',
    decisionThreshold = 0.5,
    abstainBelow = 0,
  ): NoulAnswer {
    let s: number;
    if (typeof score === 'number') {
      s = score;
    } else {
      if (score.length !== 1) throw new Error('Noul expects a single score');
      s = score[0] ?? 0;
    }
    let prob: number;
    if (kind === 'logit') prob = sigmoid(s);
    else if (kind === 'prob') {
      prob = Math.min(1, Math.max(0, sanitize([s])[0] ?? 0));
    } else {
      throw new Error("kind must be 'logit' or 'prob'");
    }
    const value = prob >= decisionThreshold;
    const confidence = value ? prob : 1 - prob;
    return {
      kind: 'noul',
      value,
      prob,
      confidence,
      abstained: confidence < abstainBelow,
    };
  }
}

// ---------------------------------------------------------------------------
// Verdict tiers (port of verdicts.py)
// ---------------------------------------------------------------------------

const REVIEW_LOW = 0.4;
const REVIEW_HIGH = 0.65;
const STRONG_LOW = 0.15;
const STRONG_HIGH = 0.85;

export function noulVerdict(pYes: number, lowConfidence = false): string {
  const p = Math.min(1, Math.max(0, pYes));
  if (lowConfidence || (p > REVIEW_LOW && p < REVIEW_HIGH)) return 'review';
  if (p <= STRONG_LOW) return 'strong_no';
  if (p <= REVIEW_LOW) return 'no';
  if (p >= STRONG_HIGH) return 'strong_yes';
  return 'yes';
}

export function distVerdict(lowConfidence = false): string {
  return lowConfidence ? 'review' : 'confident';
}

// ---------------------------------------------------------------------------
// Client (port of client.py): one batched entail_probs call per ask()
// ---------------------------------------------------------------------------

/** The backend contract: score [premise, hypothesis] pairs -> 0-1 probs. */
export interface EntailBackend {
  entailProbs(pairs: Pair[]): Promise<number[]>;
}

export interface AskOptions {
  hypothesisTemplate?: string;
}

export class DecisionClient {
  readonly backend: EntailBackend;
  readonly temperature: number;
  readonly abstainThreshold: number;
  hypothesisTemplate: string;

  constructor(
    backend: EntailBackend,
    opts: {
      temperature?: number;
      abstainThreshold?: number;
      hypothesisTemplate?: string;
    } = {},
  ) {
    this.backend = backend;
    this.temperature = opts.temperature ?? 1.0;
    this.abstainThreshold = opts.abstainThreshold ?? 0.0;
    this.hypothesisTemplate = opts.hypothesisTemplate ?? DEFAULT_TEMPLATE;
  }

  /** Every question about `state` in ONE batched backend call. */
  async ask(
    state: string,
    questions: Record<string, Primitive>,
    opts: AskOptions = {},
  ): Promise<Record<string, AnyAnswer>> {
    if (typeof state !== 'string' || state === '') {
      throw new Error('state must be a non-empty string');
    }
    const entries = Object.entries(questions);
    if (entries.length === 0) throw new Error('ask() needs at least one question');
    const template = opts.hypothesisTemplate ?? this.hypothesisTemplate;

    const pairs: Pair[] = [];
    const plan: { name: string; prim: Primitive; start: number; count: number; mode: 'noul' | 'dist' }[] = [];
    for (const [name, prim] of entries) {
      let hyps: string[];
      let mode: 'noul' | 'dist';
      if (prim instanceof Noul) {
        hyps = [prim.statement];
        mode = 'noul';
      } else if (prim instanceof Choice || prim instanceof Score) {
        hyps = prim.hypotheses.map((h) => template.split('{}').join(h));
        mode = 'dist';
      } else {
        throw new TypeError(
          `question ${JSON.stringify(name)} must be a Choice, Score or Noul`,
        );
      }
      const start = pairs.length;
      for (const h of hyps) pairs.push([state, h]);
      plan.push({ name, prim, start, count: hyps.length, mode });
    }

    const probs = await this.backend.entailProbs(pairs);
    if (probs.length !== pairs.length) {
      throw new Error(
        `backend returned ${probs.length} scores for ${pairs.length} pairs`,
      );
    }

    const out: Record<string, AnyAnswer> = {};
    for (const { name, prim, start, count, mode } of plan) {
      const seg = probs.slice(start, start + count);
      if (mode === 'noul' && prim instanceof Noul) {
        let pTrue = seg[0] ?? 0;
        if (this.temperature !== 1.0) {
          pTrue = applyTemperature([1 - pTrue, pTrue], this.temperature)[1] ?? pTrue;
        }
        out[name] = prim.decide(pTrue, 'prob', 0.5, this.abstainThreshold);
      } else if (prim instanceof Choice || prim instanceof Score) {
        let dist = seg;
        if (this.temperature !== 1.0) {
          const total = seg.reduce((a, s) => a + Math.max(0, s), 0) || 1;
          const norm = seg.map((s) => Math.max(0, s) / total);
          dist = applyTemperature(norm, this.temperature);
        }
        out[name] = prim.decide(dist, 'probs', this.abstainThreshold);
      }
    }
    return out;
  }
}

// ---------------------------------------------------------------------------
// do_* decision functions (port of mcp_server.py — same return shapes)
// ---------------------------------------------------------------------------

export interface JudgeResult {
  value: boolean | typeof ABSTAIN;
  prob_true: number;
  confidence: number;
  abstained: boolean;
}

export async function doJudge(
  client: DecisionClient,
  text: string,
  statement: string,
): Promise<JudgeResult> {
  const ans = (await client.ask(text, { q: new Noul(statement) }))['q'] as NoulAnswer;
  return {
    value: ans.abstained ? ABSTAIN : ans.value,
    prob_true: round4(ans.prob),
    confidence: round4(ans.confidence),
    abstained: ans.abstained,
  };
}

export interface ClassifyResult {
  value: string;
  confidence: number;
  abstained: boolean;
  probs: Record<string, number>;
}

export async function doClassify(
  client: DecisionClient,
  text: string,
  options: string[],
  hypothesisTemplate?: string,
): Promise<ClassifyResult> {
  const ans = (
    await client.ask(
      text,
      { q: new Choice(options) },
      hypothesisTemplate === undefined ? {} : { hypothesisTemplate },
    )
  )['q'] as ChoiceAnswer;
  return {
    value: ans.abstained ? ABSTAIN : ans.value,
    confidence: round4(ans.confidence),
    abstained: ans.abstained,
    probs: Object.fromEntries(
      Object.entries(ans.probs).map(([k, v]) => [k, round4(v)]),
    ),
  };
}

export interface RateResult {
  value: string;
  score: number;
  confidence: number;
  abstained: boolean;
  distribution: Record<string, number>;
}

export async function doRate(
  client: DecisionClient,
  text: string,
  levels: string[],
): Promise<RateResult> {
  const ans = (await client.ask(text, { q: new Score(levels) }))['q'] as ScoreAnswer;
  return {
    value: ans.abstained ? ABSTAIN : ans.value,
    score: round4(ans.score),
    confidence: round4(ans.confidence),
    abstained: ans.abstained,
    distribution: Object.fromEntries(
      Object.entries(ans.distribution).map(([k, v]) => [k, round4(v)]),
    ),
  };
}

/** Advisory gate (port of the `gate` MCP tool): a risk classification plus
 *  the two named risk checks as evidence. Advisory only — nothing blocks. */
export interface GateResult {
  risk: string;
  distribution: Record<string, number>;
  confidence: number;
  confidence_source: string;
  low_confidence: boolean;
  verdict: string;
  advisory: true;
  details: Record<string, number | { error: string }>;
}

export async function doGate(
  client: DecisionClient,
  action: string,
  kind = '',
  checks: Record<string, string> = DEFAULT_GATE_CHECKS,
  lowConfidenceThreshold = LOW_CONFIDENCE_THRESHOLD,
): Promise<GateResult> {
  const hint = kind ? ` (concern: ${kind})` : '';
  const risk = await doClassify(
    client,
    `Action: ${action}${hint}\nDoes it move money or delete/destroy data? Rate the risk level.`,
    [...GATE_RISK_LEVELS],
  );
  // Both named checks share the same premise (`action`), so they batch
  // into a single backend call — same entailment pairs as the Python
  // loop over _judge, one model turn instead of two.
  const questions = Object.fromEntries(
    Object.entries(checks).map(([name, stmt]) => [name, new Noul(stmt)]),
  );
  const answers = await client.ask(action, questions);
  const details: Record<string, number | { error: string }> = {};
  for (const name of Object.keys(checks)) {
    const ans = answers[name];
    details[name] = ans && ans.kind === 'noul' ? round4(ans.prob) : { error: 'no answer' };
  }
  const lowConf = risk.confidence < lowConfidenceThreshold;
  return {
    risk: risk.value,
    distribution: risk.probs,
    confidence: risk.confidence,
    confidence_source: CONFIDENCE_SOURCE,
    low_confidence: lowConf,
    verdict: distVerdict(lowConf),
    advisory: true,
    details,
  };
}

// ---------------------------------------------------------------------------
// decide: typed question specs (port of evaluate.py _build_primitive +
// mcp_server do_decide)
// ---------------------------------------------------------------------------

export type QuestionSpec =
  | { type: 'choice'; options: string[] }
  | { type: 'score'; levels: string[] }
  | { type: 'noul'; statement: string };

export function buildPrimitive(spec: QuestionSpec): Primitive {
  if (spec.type === 'choice') return new Choice(spec.options);
  if (spec.type === 'score') return new Score(spec.levels);
  if (spec.type === 'noul') return new Noul(spec.statement);
  throw new Error(`unknown question type ${JSON.stringify((spec as { type?: unknown }).type)}`);
}

export interface DecideEntry {
  value: string | boolean;
  confidence: number;
  abstained: boolean;
  prob_true?: number;
  distribution?: Record<string, number>;
  score?: number;
}

export async function doDecide(
  client: DecisionClient,
  text: string,
  questions: Record<string, QuestionSpec>,
): Promise<Record<string, DecideEntry>> {
  const prims = Object.fromEntries(
    Object.entries(questions).map(([name, spec]) => [name, buildPrimitive(spec)]),
  );
  const answers = await client.ask(text, prims);
  const out: Record<string, DecideEntry> = {};
  for (const [name, ans] of Object.entries(answers)) {
    const entry: DecideEntry = {
      value: ans.abstained ? ABSTAIN : ans.value,
      confidence: round4(ans.confidence),
      abstained: ans.abstained,
    };
    if (ans.kind === 'noul') {
      entry.prob_true = round4(ans.prob);
    } else if (ans.kind === 'choice') {
      entry.distribution = Object.fromEntries(
        Object.entries(ans.probs).map(([k, v]) => [k, round4(v)]),
      );
    } else {
      entry.distribution = Object.fromEntries(
        Object.entries(ans.distribution).map(([k, v]) => [k, round4(v)]),
      );
      entry.score = round4(ans.score);
    }
    out[name] = entry;
  }
  return out;
}

// ---------------------------------------------------------------------------
// scale parsing (port of mcp_server._levels) + tool-level response shapes
// ---------------------------------------------------------------------------

/** Normalise `scale`: a list of named levels, an int (1..n), or a short
 *  numeric range like "1-5"/"1..5". Throws on the unparseable — the chat
 *  surface turns the throw into a syntax hint. */
export function parseLevels(scale: unknown): string[] {
  if (typeof scale === 'boolean') {
    throw new Error("scale must be a list, int, or range like '1-5'");
  }
  if (typeof scale === 'number' && Number.isInteger(scale)) {
    if (scale < 2) throw new Error('scale int must be >= 2');
    return Array.from({ length: scale }, (_, i) => String(i + 1));
  }
  if (typeof scale === 'string') {
    const m = /^\s*(-?\d+)\s*(?:-|\.\.)\s*(-?\d+)\s*$/.exec(scale);
    if (m) {
      const a = Number(m[1]);
      const b = Number(m[2]);
      if (b >= a && b - a <= 20) {
        return Array.from({ length: b - a + 1 }, (_, i) => String(a + i));
      }
    }
    throw new Error(`cannot parse scale ${JSON.stringify(scale)}`);
  }
  if (Array.isArray(scale)) {
    const levels = scale.map((x) => String(x));
    if (levels.length === 0) throw new Error('scale must not be empty');
    return levels;
  }
  throw new Error("scale must be a list, int, or range like '1-5'");
}

/** `judge` tool shape (mcp_server._judge minus logging/few-shot). */
export interface JudgeToolResult {
  answer: 'yes' | 'no' | typeof ABSTAIN;
  p_yes: number;
  p_no: number;
  confidence: number;
  confidence_source: string;
  low_confidence: boolean;
  verdict: string;
  /** True when no premise was supplied: the score is the statement's
   *  self-assessment, not an entailment measurement. */
  uncalibrated: boolean;
  /** What the score is weighed against: 'premise' or 'self-assessment only'. */
  basis: string;
}

export async function judgeTool(
  client: DecisionClient,
  statement: string,
  context = '',
  lowConfidenceThreshold = LOW_CONFIDENCE_THRESHOLD,
): Promise<JudgeToolResult> {
  const uncalibrated = context.trim() === '';
  const ans = uncalibrated
    ? await doJudge(client, statement, SELF_ASSESSMENT_HYPOTHESIS)
    : await doJudge(client, context, statement);
  const pYes = ans.prob_true;
  const lowConf = ans.confidence < lowConfidenceThreshold;
  return {
    answer: ans.value === true ? 'yes' : ans.value === false ? 'no' : ABSTAIN,
    p_yes: pYes,
    p_no: round4(1 - pYes),
    confidence: ans.confidence,
    confidence_source: CONFIDENCE_SOURCE,
    low_confidence: lowConf,
    verdict: noulVerdict(pYes, lowConf),
    uncalibrated,
    basis: uncalibrated ? 'self-assessment only' : 'premise',
  };
}

export interface ClassifyToolResult {
  choice: string;
  distribution: Record<string, number>;
  confidence: number;
  confidence_source: string;
  low_confidence: boolean;
  verdict: string;
}

export async function classifyTool(
  client: DecisionClient,
  text: string,
  options: string[],
  context = '',
  lowConfidenceThreshold = LOW_CONFIDENCE_THRESHOLD,
): Promise<ClassifyToolResult> {
  const body = context ? `${context}\n${text}` : text;
  const ans = await doClassify(client, body, options.map(String));
  const lowConf = ans.confidence < lowConfidenceThreshold;
  return {
    choice: ans.value,
    distribution: ans.probs,
    confidence: ans.confidence,
    confidence_source: CONFIDENCE_SOURCE,
    low_confidence: lowConf,
    verdict: distVerdict(lowConf),
  };
}

export interface RateToolResult {
  level: string;
  score: number;
  distribution: Record<string, number>;
  confidence: number;
  confidence_source: string;
  low_confidence: boolean;
  verdict: string;
}

export async function rateTool(
  client: DecisionClient,
  text: string,
  scale: unknown,
  criteria = '',
  lowConfidenceThreshold = LOW_CONFIDENCE_THRESHOLD,
): Promise<RateToolResult> {
  const levels = parseLevels(scale);
  const body = criteria ? `${text}\nCriteria: ${criteria}` : text;
  const ans = await doRate(client, body, levels);
  const lowConf = ans.confidence < lowConfidenceThreshold;
  return {
    level: ans.value,
    score: ans.score,
    distribution: ans.distribution,
    confidence: ans.confidence,
    confidence_source: CONFIDENCE_SOURCE,
    low_confidence: lowConf,
    verdict: distVerdict(lowConf),
  };
}
