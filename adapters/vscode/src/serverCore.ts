/**
 * Scoring endpoint core — a pure request handler testable without
 * node:http. The wire contract is defined by the Python consumer,
 * `src/poordjaevin/backends/vscode_lm.py`:
 *
 *   POST <url>  Authorization: Bearer <token>
 *   {"pairs": [[premise, hypothesis], ...]} -> {"probs": [0-100...], "model"}
 *   {"warmup": true}                        -> {"warmup": true, "model"}
 *
 *   wrong path     -> 404
 *   wrong auth     -> 401
 *   bad JSON/shape -> 400
 *   no model       -> 503 {"error": ...}   (fallback-eligible upstream)
 *   other failures -> 200 {"error": ...}   (protocol error upstream)
 *
 * Like djaevin-acp-bridge.mjs's parentQueue, requests are serialized end
 * to end — one scoring call at a time.
 */

import { NoModelError, validatePairs } from './scoring.ts';
import type { Pair } from './scoring.ts';

export interface HttpRequestLike {
  method: string;
  path: string;
  headers: Record<string, string | string[] | undefined>;
  /** Raw request body as text (already size-capped by the transport). */
  body: string;
}

export interface CoreResponse {
  status: number;
  json: Record<string, unknown>;
}

/** What the handler needs from the extension host. `scorePairs` returns
 *  self-reported 0-100 scores (the wire contract — the Python backend
 *  divides by 100). */
export interface ServerCoreDeps {
  token: string;
  getModel(): string | null;
  scorePairs(pairs: Pair[]): Promise<{ probs: number[]; model: string | null }>;
}

function header(
  headers: HttpRequestLike['headers'],
  name: string,
): string | undefined {
  const v = headers[name.toLowerCase()];
  return Array.isArray(v) ? v[0] : v;
}

async function handle(
  req: HttpRequestLike,
  deps: ServerCoreDeps,
): Promise<CoreResponse> {
  if (req.path !== '/score') {
    return { status: 404, json: { error: 'not found' } };
  }
  if (req.method !== 'POST') {
    return { status: 405, json: { error: 'method not allowed' } };
  }
  if (header(req.headers, 'authorization') !== `Bearer ${deps.token}`) {
    return { status: 401, json: { error: 'unauthorized' } };
  }
  let payload: unknown;
  try {
    payload = JSON.parse(req.body);
  } catch {
    return { status: 400, json: { error: 'bad json' } };
  }
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) {
    return { status: 400, json: { error: 'bad json' } };
  }
  const body = payload as Record<string, unknown>;
  // Truthy, not === true: both the ACP bridge and the Python fake handler
  // check `payload.get("warmup")` by truthiness, and warmup wins even when
  // `pairs` is also present.
  if (body['warmup']) {
    return { status: 200, json: { warmup: true, model: deps.getModel() } };
  }
  let pairs: Pair[];
  try {
    pairs = validatePairs(body['pairs']);
  } catch (error) {
    return {
      status: 400,
      json: { error: error instanceof Error ? error.message : String(error) },
    };
  }
  try {
    const { probs, model } = await deps.scorePairs(pairs);
    return { status: 200, json: { probs, model } };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    // Unavailability is the only 503; anything else is a protocol error
    // carried in a 200 so the Python side raises RuntimeError, never
    // VscodeLmUnavailable (a malformed reply is a bug, not a fallback).
    if (error instanceof NoModelError) {
      return { status: 503, json: { error: message } };
    }
    return { status: 200, json: { error: message } };
  }
}

export interface ServerCore {
  handleRequest(req: HttpRequestLike): Promise<CoreResponse>;
}

/** Serialized handler: requests queue behind each other exactly like the
 *  ACP bridge serializes parent requests — one model turn at a time. */
export function createServerCore(deps: ServerCoreDeps): ServerCore {
  let queue: Promise<unknown> = Promise.resolve();
  return {
    handleRequest(req) {
      const result = queue.then(() => handle(req, deps));
      queue = result.catch(() => undefined);
      return result;
    },
  };
}
