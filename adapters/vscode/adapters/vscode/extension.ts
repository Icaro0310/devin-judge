/**
 * Extension entry — judge (icaro0310.judge).
 * Wiring only: starts the localhost scoring endpoint (the surface the
 * poordjaevin `vscode-lm` Python backend consumes), registers the
 * `@judge` chat participant and the judge.* commands.
 *
 * Consent contract: `vscode.lm.selectChatModels` can show a model-access
 * consent dialog that requires a user gesture, so nothing here calls it
 * at activation. The endpoint listens and advertises itself without a
 * model; the first /score request (or @judge message) selects lazily.
 */

import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as vscode from 'vscode';
import {
  loadCalibration,
  resolveCalibratorPath,
  resolveForBackend,
} from '../../src/calibration.ts';
import type { RuntimeCalibration } from '../../src/calibration.ts';
import {
  endpointFilePath,
  removeEndpointFile,
  writeEndpointFile,
} from '../../src/endpointFile.ts';
import { createJudgeParticipant } from './chat.ts';
import { createLmScorer } from './lmScorer.ts';
import type { ModelSelector } from './lmScorer.ts';
import { startScoreServer } from './server.ts';
import type { RunningEndpoint } from './server.ts';

const EXTENSION_ID = 'icaro0310.judge';
const SLASH_COMMANDS = ['judge', 'classify', 'rate', 'gate', 'decide'] as const;

// Held for deactivate(): the listening endpoint and its discovery file.
let running: RunningEndpoint | null = null;
let advertisedFile: string | null = null;

export function activate(context: vscode.ExtensionContext): Promise<void> {
  const output = vscode.window.createOutputChannel('Judge');
  context.subscriptions.push(output);
  const version = String(context.extension.packageJSON.version ?? '0.0.0');

  const getConfig = (): vscode.WorkspaceConfiguration =>
    vscode.workspace.getConfiguration('judge');

  const modelSelector = (): ModelSelector => {
    const cfg = getConfig();
    const out: ModelSelector = {};
    const vendor = cfg.get<string>('model.vendor', '').trim();
    const family = cfg.get<string>('model.family', '').trim();
    const id = cfg.get<string>('model.id', '').trim();
    if (vendor) out.vendor = vendor;
    if (family) out.family = family;
    if (id) out.id = id;
    return out;
  };

  const scorer = createLmScorer(modelSelector);

  // Fresh read per request — `poordjaevin calibrate` rewrites the file and
  // a re-read is the cheapest honest update. Calibration warnings surface
  // once per request in the output channel, not as popups.
  const calibration = (): RuntimeCalibration => {
    const configured = getConfig().get<string>(
      'calibratorPath',
      'calibration.json',
    );
    const workspaceDir = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    const resolved = resolveCalibratorPath(configured, workspaceDir);
    const rt = resolveForBackend(loadCalibration(resolved), 'self_report');
    for (const w of rt.warnings) output.appendLine(`[calibration] ${w}`);
    return rt;
  };

  context.subscriptions.push(
    createJudgeParticipant({ scorer, calibration }),
  );

  // Minimal wrappers: prefill `@judge /<cmd>` in the chat input so the
  // participant (and its model consent flow) does the real work. Typing
  // the query without submitting keeps the human in the loop.
  const openJudgeChat = async (query: string): Promise<void> => {
    await vscode.commands.executeCommand('workbench.action.chat.open', {
      query,
      isPartialQuery: true,
    });
    for (const command of ['workbench.action.chat.focus']) {
      try {
        await vscode.commands.executeCommand(command);
        break;
      } catch {
        // Some VS Code builds expose the chat open command without a
        // dedicated focus command; the open command itself already brings
        // the chat view into view. Fall through without aborting.
      }
    }
  };

  for (const name of SLASH_COMMANDS) {
    context.subscriptions.push(
      vscode.commands.registerCommand(`judge.${name}`, async () => {
        await openJudgeChat(`@judge /${name} `);
      }),
    );
  }

  // Endpoint: listen first, advertise after — the file must never name a
  // port that is not yet accepting connections.
  const ready = (async () => {
    if (!getConfig().get<boolean>('endpoint.enabled', true)) {
      output.appendLine(
        'endpoint disabled via judge.endpoint.enabled=false',
      );
      return;
    }
    try {
      const token = crypto.randomBytes(24).toString('hex');
      const endpoint = await startScoreServer({
        token,
        getModel: () => scorer.getLastModel(),
        scorePairs: (pairs) => scorer.scorePairs(pairs),
      });
      running = endpoint;
      const file = endpointFilePath();
      try {
        writeEndpointFile(file, {
          url: `http://127.0.0.1:${endpoint.port}/score`,
          token,
          pid: process.pid,
          extension: EXTENSION_ID,
          version,
        });
      } catch (writeError) {
        // No discovery file means the listener is unreachable but still
        // holds a port: fail closed rather than run an orphan endpoint.
        running = null;
        await endpoint.close();
        throw writeError;
      }
      advertisedFile = file;
      output.appendLine(
        `vscode-lm endpoint on 127.0.0.1:${endpoint.port} (advertised in ${file})`,
      );
    } catch (error) {
      output.appendLine(
        `endpoint failed to start: ${
          error instanceof Error ? error.message : String(error)
        }`,
      );
    }
  })();

  return ready;
}

export async function deactivate(): Promise<void> {
  // Un-advertise first so consumers stop dialing, then stop listening.
  // Another window may own the advertised file — only remove what we wrote.
  const file = advertisedFile;
  advertisedFile = null;
  if (file) {
    try {
      const spec = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (spec.pid === process.pid) removeEndpointFile(file);
    } catch {
      // unreadable file: leave it, a stale spec fails closed upstream
    }
  }
  const endpoint = running;
  running = null;
  if (endpoint) await endpoint.close();
}
