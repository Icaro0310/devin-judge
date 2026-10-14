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

/** Write the endpoint spec (mkdir + fd-write + fchmod). The file carries a
 *  bearer token, so it must be 0o600 the whole time: open the fd first,
 *  fchmod it, and only then write the token. A pre-existing permissive file
 *  is restricted before a single new byte lands; a symlinked path is
 *  replaced by a real file rather than followed, so the token cannot be
 *  redirected to an unintended target. */
export function writeEndpointFile(file: string, spec: EndpointSpec): void {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  try {
    if (fs.lstatSync(file).isSymbolicLink()) {
      fs.unlinkSync(file);
    }
  } catch {
    // missing path is the common case
  }
  const fd = fs.openSync(file, 'w', 0o600);
  try {
    fs.fchmodSync(fd, 0o600);
    fs.writeSync(fd, `${JSON.stringify(spec)}\n`, null, 'utf8');
  } finally {
    fs.closeSync(fd);
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
