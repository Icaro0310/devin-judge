/**
 * lmScorer — the only module that touches `vscode.lm`. Turns
 * [premise, hypothesis] pairs into self-reported 0-100 scores using the
 * exact djaevin-acp-bridge prompt, via whichever chat model VS Code is
 * serving (Copilot, Codex, BYOK — request.model wins when set).
 *
 * Consent contract: selectChatModels is only ever called lazily, inside
 * a request — never at activation — because it can trigger the model
 * access consent dialog, which requires a user gesture.
 */

import * as vscode from 'vscode';
import {
  buildPrompt,
  NoModelError,
  parseScores,
  validatePairs,
} from '../../src/scoring.ts';
import type { Pair } from '../../src/scoring.ts';

export interface ModelSelector {
  vendor?: string;
  family?: string;
  id?: string;
}

/** LanguageModelError codes that mean "no model can serve this" — they
 *  surface as HTTP 503, the only fallback-eligible signal upstream. */
const UNAVAILABLE_CODES = new Set(['NoPermissions', 'NotFound', 'Blocked']);

const JUSTIFICATION =
  'poordjaevin needs the model to score entailment for calibrated decisions';

function modelLabel(model: vscode.LanguageModelChat): string {
  const vf = [model.vendor, model.family].filter(Boolean).join('/');
  return vf || model.name || model.id || 'vscode-lm';
}

export interface LmScorer {
  /** Self-reported 0-100 scores — the wire contract the Python
   *  vscode-lm backend divides by 100. */
  scorePairs(
    pairs: Pair[],
    preferredModel?: vscode.LanguageModelChat,
    token?: vscode.CancellationToken,
  ): Promise<{ probs: number[]; model: string }>;
  /** Same scores normalized to 0-1 entailment probabilities — the
   *  DecisionClient backend contract. */
  entailProbs(
    pairs: Pair[],
    preferredModel?: vscode.LanguageModelChat,
    token?: vscode.CancellationToken,
  ): Promise<number[]>;
  /** Last model that actually served a request (for warmup replies). */
  getLastModel(): string | null;
}

export function createLmScorer(
  getSelector: () => ModelSelector,
): LmScorer {
  let lastModel: string | null = null;

  async function pick(
    preferred?: vscode.LanguageModelChat,
  ): Promise<vscode.LanguageModelChat> {
    if (preferred) return preferred;
    let models: vscode.LanguageModelChat[];
    try {
      models = await vscode.lm.selectChatModels(getSelector());
    } catch (error) {
      if (
        error instanceof vscode.LanguageModelError &&
        UNAVAILABLE_CODES.has(error.code)
      ) {
        throw new NoModelError(
          `chat model selection failed (${error.code}): ${error.message}`,
        );
      }
      throw error;
    }
    const first = models[0];
    if (!first) {
      throw new NoModelError(
        'no chat model available: pick one in VS Code chat or install Copilot/BYOK',
      );
    }
    return first;
  }

  async function scorePairs(
    pairs: Pair[],
    preferredModel?: vscode.LanguageModelChat,
    token?: vscode.CancellationToken,
  ): Promise<{ probs: number[]; model: string }> {
    const checked = validatePairs(pairs);
    const model = await pick(preferredModel);
    const messages = [vscode.LanguageModelChatMessage.User(buildPrompt(checked))];
    let text = '';
    try {
      const response = await model.sendRequest(
        messages,
        { justification: JUSTIFICATION },
        token,
      );
      for await (const fragment of response.text) {
        text += fragment;
      }
    } catch (error) {
      if (
        error instanceof vscode.LanguageModelError &&
        UNAVAILABLE_CODES.has(error.code)
      ) {
        throw new NoModelError(
          `chat model unavailable (${error.code}): ${error.message}`,
        );
      }
      throw error;
    }
    lastModel = modelLabel(model);
    return { probs: parseScores(text, checked.length), model: lastModel };
  }

  return {
    scorePairs,
    async entailProbs(pairs, preferredModel, token) {
      const { probs } = await scorePairs(pairs, preferredModel, token);
      return probs.map((p) => Math.min(1, Math.max(0, p / 100)));
    },
    getLastModel: () => lastModel,
  };
}
