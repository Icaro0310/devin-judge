/**
 * `@judge` chat participant — vscode glue only. Model resolution
 * (request.model wins, else the configured selector) and calibration
 * loading happen here; parsing, deciding and rendering live in the pure
 * chatModel.ts. Errors never leak: a broken answer degrades to an
 * honest message.
 */

import * as vscode from 'vscode';
import { DecisionClient } from '../../src/decisions.ts';
import { handleChat } from './chatModel.ts';
import type { LmScorer } from './lmScorer.ts';
import type { Pair } from '../../src/scoring.ts';

/** Must match `contributes.chatParticipants[].id` in package.json. */
const PARTICIPANT_ID = 'judge.judge';
const FALLBACK =
  'judge could not produce a decision; see the Judge output channel';

export interface JudgeEnv {
  scorer: LmScorer;
  /** Resolved once per request — the calibrator file is tiny, and a
   *  fresh read picks up a re-run `poordjaevin calibrate` for free. */
  calibration(): { temperature: number; abstainThreshold: number };
}

export function createJudgeParticipant(env: JudgeEnv): vscode.ChatParticipant {
  const handler: vscode.ChatRequestHandler = async (
    request,
    _context,
    stream,
    token,
  ) => {
    try {
      const entail = (pairs: Pair[]) =>
        env.scorer.entailProbs(pairs, request.model ?? undefined, token);
      const cal = env.calibration();
      const client = new DecisionClient(
        { entailProbs: entail },
        { temperature: cal.temperature, abstainThreshold: cal.abstainThreshold },
      );
      stream.markdown(
        await handleChat(request.command, request.prompt, client, {
          temperature: cal.temperature,
        }),
      );
    } catch (error) {
      try {
        stream.markdown(
          `**judge** could not answer: ${
            error instanceof Error ? error.message : String(error)
          }`,
        );
      } catch {
        stream.markdown(FALLBACK);
      }
    }
    return {};
  };
  return vscode.chat.createChatParticipant(PARTICIPANT_ID, handler);
}
