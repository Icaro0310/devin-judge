/**
 * Endpoint discovery file — port of the platform data-dir logic in
 * `src/poordjaevin/backends/vscode_lm.py::_default_endpoint_file`. The
 * extension writes `<data-dir>/poordjaevin/vscode-lm-endpoint.json` after
 * the HTTP server starts listening; the Python backend re-reads it on
 * every request, so a restart on a new port is picked up for free.
 *
 * The file holds a bearer token, so it is always written mode 0o600.
 * Pure: node:fs/node:os/node:path only, no vscode.
 */

import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

export interface EndpointSpec {
  url: string;
  token: string;
  pid: number;
  extension: string;
  version: string;
}

export interface EndpointEnv {
  platform?: string;
  env?: NodeJS.ProcessEnv;
  home?: string;
}

/** Mirrors _default_endpoint_file: win -> LOCALAPPDATA|APPDATA|~/AppData/Local,
 *  darwin -> ~/Library/Application Support, else XDG_DATA_HOME|~/.local/share,
 *  always + poordjaevin/vscode-lm-endpoint.json. */
export function endpointFilePath(opts: EndpointEnv = {}): string {
  const platform = opts.platform ?? process.platform;
  const env = opts.env ?? process.env;
  const home = opts.home ?? os.homedir();
  let base: string;
  if (platform === 'win32' || platform.startsWith('win')) {
    base =
      env['LOCALAPPDATA'] ||
      env['APPDATA'] ||
      path.join(home, 'AppData', 'Local');
  } else if (platform === 'darwin') {
    base = path.join(home, 'Library', 'Application Support');
  } else {
    base = env['XDG_DATA_HOME'] || path.join(home, '.local', 'share');
  }
  return path.join(base, 'poordjaevin', 'vscode-lm-endpoint.json');
}

/** Write the endpoint spec atomically, designed against three concrete
 *  failure classes found in review:
 *
 *  1. Permissive overwrite window — writing token bytes into an existing
 *     0o644 file before chmod lands exposes the credential. Fixed by never
 *     writing to the final path: the token only ever lives in a file that
 *     was created O_EXCL with mode 0o600, then rename(2) swaps the path
 *     atomically. There is no state where a permissive file holds a token.
 *  2. Discovery-path symlink — a symlinked target would be followed and
 *     the token written elsewhere. rename() replaces the symlink itself
 *     with the real file; the target is never opened.
 *  3. Predictable temp path — a `tmp-<pid>` name could be pre-planted as a
 *     symlink or permissive file. The temp name is crypto-random and
 *     opened 'wx' (O_EXCL): it can never follow or extend an existing
 *     path, on any filesystem, with no dependence on chmod working.
 */
export function writeEndpointFile(file: string, spec: EndpointSpec): void {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.tmp-${crypto.randomBytes(6).toString('hex')}`;
  const fd = fs.openSync(tmp, 'wx', 0o600);
  try {
    try {
      fs.writeSync(fd, `${JSON.stringify(spec)}\n`, null, 'utf8');
    } finally {
      fs.closeSync(fd);
    }
    fs.renameSync(tmp, file);
  } finally {
    try {
      fs.unlinkSync(tmp);
    } catch {
      // already renamed, or the write failed before tmp existed
    }
  }
}

/** Remove the endpoint file on deactivate; missing file is fine. */
export function removeEndpointFile(file: string): void {
  try {
    fs.unlinkSync(file);
  } catch {
    // already gone
  }
}
