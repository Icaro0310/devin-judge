import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  endpointFilePath,
  removeEndpointFile,
  writeEndpointFile,
} from '../src/endpointFile.ts';

// Mirrors test_vscode_lm_backend.py::test_default_endpoint_file_platform_roots.

test('endpointFilePath: linux uses XDG_DATA_HOME or ~/.local/share', () => {
  const home = '/home/u';
  assert.equal(
    endpointFilePath({ platform: 'linux', env: {}, home }),
    path.join(home, '.local', 'share', 'poordjaevin', 'vscode-lm-endpoint.json'),
  );
  assert.equal(
    endpointFilePath({ platform: 'linux', env: { XDG_DATA_HOME: '/xdg' }, home }),
    path.join('/xdg', 'poordjaevin', 'vscode-lm-endpoint.json'),
  );
});

test('endpointFilePath: darwin uses ~/Library/Application Support', () => {
  const p = endpointFilePath({ platform: 'darwin', env: {}, home: '/Users/u' });
  assert.ok(p.includes(path.join('Library', 'Application Support')));
  assert.ok(p.endsWith(path.join('poordjaevin', 'vscode-lm-endpoint.json')));
});

test('endpointFilePath: win32 prefers LOCALAPPDATA, falls back APPDATA/home', () => {
  assert.equal(
    endpointFilePath({
      platform: 'win32',
      env: { LOCALAPPDATA: 'C:\\local' },
      home: 'C:\\Users\\u',
    }),
    path.join('C:\\local', 'poordjaevin', 'vscode-lm-endpoint.json'),
  );
  assert.equal(
    endpointFilePath({
      platform: 'win32',
      env: { APPDATA: 'C:\\roaming' },
      home: 'C:\\Users\\u',
    }),
    path.join('C:\\roaming', 'poordjaevin', 'vscode-lm-endpoint.json'),
  );
  const fallback = endpointFilePath({
    platform: 'win32',
    env: {},
    home: 'C:\\Users\\u',
  });
  assert.ok(fallback.startsWith(path.join('C:\\Users\\u', 'AppData', 'Local')));
});

test('writeEndpointFile + removeEndpointFile roundtrip with mode 0o600', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'judge-ep-'));
  const file = path.join(dir, 'nested', 'vscode-lm-endpoint.json');
  const spec = {
    url: 'http://127.0.0.1:1234/score',
    token: 't'.repeat(48),
    pid: process.pid,
    extension: 'icaro0310.judge',
    version: '0.1.0',
  };
  writeEndpointFile(file, spec);
  const read = JSON.parse(fs.readFileSync(file, 'utf8'));
  assert.deepEqual(read, spec);
  if (process.platform !== 'win32') {
    assert.equal(fs.statSync(file).mode & 0o777, 0o600, 'token file is 0600');
  }
  // overwrite keeps the mode tight
  writeEndpointFile(file, { ...spec, token: 'x'.repeat(48) });
  if (process.platform !== 'win32') {
    assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  }
  removeEndpointFile(file);
  assert.equal(fs.existsSync(file), false);
  removeEndpointFile(file); // idempotent
  fs.rmSync(dir, { recursive: true, force: true });
});

test('writeEndpointFile restricts a pre-existing permissive file', () => {
  if (process.platform === 'win32') return; // POSIX modes only
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'judge-ep-'));
  const file = path.join(dir, 'vscode-lm-endpoint.json');
  fs.writeFileSync(file, '{"stale":true}', { mode: 0o644 });
  assert.equal(fs.statSync(file).mode & 0o777, 0o644);
  writeEndpointFile(file, {
    url: 'http://127.0.0.1:1/score', token: 'secret', pid: 1,
    extension: 'icaro0310.judge', version: '0.0.0',
  });
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).token, 'secret');
  fs.rmSync(dir, { recursive: true, force: true });
});

test('writeEndpointFile replaces a symlink instead of following it', () => {
  if (process.platform === 'win32') return; // needs symlink privilege
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'judge-ep-'));
  const target = path.join(dir, 'target.json');
  fs.writeFileSync(target, '{"keep":true}');
  const file = path.join(dir, 'vscode-lm-endpoint.json');
  fs.symlinkSync(target, file);
  writeEndpointFile(file, {
    url: 'http://127.0.0.1:1/score', token: 'secret', pid: 1,
    extension: 'icaro0310.judge', version: '0.0.0',
  });
  assert.equal(fs.lstatSync(file).isSymbolicLink(), false);
  assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).token, 'secret');
  assert.deepEqual(JSON.parse(fs.readFileSync(target, 'utf8')), { keep: true });
  fs.rmSync(dir, { recursive: true, force: true });
});
