/**
 * Calibration — pure TypeScript port of the parts of poordjaevin's
 * calibration.py + mcp_server.load_calibration that apply at decision
 * time: temperature rescaling of a distribution, and loading a fitted
 * calibrator file ({temperature, abstain_threshold, backend}).
 *
 * Filesystem reads only (node:fs); no vscode imports.
 */

import * as fs from 'node:fs';
import * as path from 'node:path';

/** softmax(log p / T). Port of calibration.apply_temperature: T = 1 is a
 *  no-op, T > 1 softens, T < 1 sharpens; argmax is preserved. */
export function applyTemperature(dist: readonly number[], T: number): number[] {
  if (T <= 0) throw new Error('temperature must be > 0');
  const logits = dist.map((p) => Math.log(Math.max(p, 1e-12)) / T);
  const m = Math.max(...logits);
  const exps = logits.map((l) => Math.exp(l - m));
  const s = exps.reduce((a, b) => a + b, 0);
  return exps.map((e) => e / s);
}

export interface Calibration {
  temperature: number;
  abstainThreshold: number;
  backend: string | null;
  warnings: string[];
}

const KNOWN_BACKENDS = new Set(['local_nli', 'logprobs', 'self_report']);

/** Port of mcp_server.load_calibration: read a calibrator file, validate
 *  ranges, honour the POORDJAEVIN_ABSTAIN opt-in (abstention is off by
 *  default, exactly like the Python server). Never throws: a missing or
 *  malformed calibrator falls back to raw confidence + a warning. */
export function loadCalibration(
  calibratorPath: string | null | undefined,
  env: NodeJS.ProcessEnv = process.env,
): Calibration {
  const warnings: string[] = [];
  const defaults: Calibration = {
    temperature: 1.0,
    abstainThreshold: 0.0,
    backend: null,
    warnings,
  };
  if (!calibratorPath || !fs.existsSync(calibratorPath)) {
    if (calibratorPath) {
      warnings.push(`no calibrator at ${calibratorPath}; using raw confidence`);
    }
    return defaults;
  }
  try {
    const saved = JSON.parse(fs.readFileSync(calibratorPath, 'utf8')) as Record<
      string,
      unknown
    >;
    let temperature = Number(saved['temperature'] ?? 1.0);
    let threshold = Number(saved['abstain_threshold'] ?? 0.0);
    let backend = (saved['backend'] as string | undefined) ?? 'local_nli';
    if (!(temperature > 0 && temperature < Infinity)) temperature = 1.0;
    if (!(threshold >= 0 && threshold <= 1.01)) threshold = 0.0;
    const abstainEnabled =
      (env['POORDJAEVIN_ABSTAIN'] ?? 'off').trim().toLowerCase() === 'on';
    if (!abstainEnabled) threshold = 0.0;
    if (!KNOWN_BACKENDS.has(backend)) backend = 'unknown';
    return { temperature, abstainThreshold: threshold, backend, warnings };
  } catch {
    return defaults;
  }
}

export interface RuntimeCalibration {
  temperature: number;
  abstainThreshold: number;
  warnings: string[];
}

/** Port of the backend-match check in mcp_server.build_client: a
 *  calibrator fitted for a different confidence source must not be
 *  applied — raw confidence beats wrong calibration. This extension is
 *  always `self_report` (the vscode-lm backend's confidence_source). A
 *  calibrator with no `backend` field defaults to 'local_nli' upstream,
 *  so it mismatches here on purpose. */
export function resolveForBackend(
  cal: Calibration,
  source = 'self_report',
): RuntimeCalibration {
  const warnings = [...cal.warnings];
  if (cal.backend && cal.backend !== source) {
    warnings.push(
      `calibration backend ${cal.backend} does not match ${source}; using raw confidence.`,
    );
    return { temperature: 1.0, abstainThreshold: 0.0, warnings };
  }
  return {
    temperature: cal.temperature,
    abstainThreshold: cal.abstainThreshold,
    warnings,
  };
}

/** Resolve `devin-judge.calibratorPath`: absolute stays absolute, a
 *  relative path resolves against the workspace folder (extension-host
 *  cwd when there is none — same semantics as the Python default). */
export function resolveCalibratorPath(
  configured: string | null | undefined,
  workspaceDir: string | null | undefined,
): string {
  const p = configured?.trim() || 'calibration.json';
  if (path.isAbsolute(p)) return p;
  return path.resolve(workspaceDir ?? process.cwd(), p);
}
