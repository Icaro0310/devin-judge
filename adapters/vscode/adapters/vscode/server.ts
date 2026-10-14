/**
 * Scoring endpoint transport — node:http on 127.0.0.1 port 0. All the
 * semantics live in src/serverCore.ts; this file only adapts real HTTP
 * to the pure handler (body collection with a size cap, JSON response).
 * One request at a time is enforced by the serialized core.
 */

import * as http from 'node:http';
import { createServerCore } from '../../src/serverCore.ts';
import type { ServerCoreDeps } from '../../src/serverCore.ts';

/** Pairs are small text batches; 4 MiB is far beyond any sane payload. */
const MAX_BODY_BYTES = 4 * 1024 * 1024;

export interface RunningEndpoint {
  port: number;
  close(): Promise<void>;
}

export function startScoreServer(
  deps: ServerCoreDeps,
): Promise<RunningEndpoint> {
  const core = createServerCore(deps);
  const server = http.createServer((req, res) => {
    const chunks: Buffer[] = [];
    let size = 0;
    let replied = false;
    const reply = (status: number, json: unknown): void => {
      if (replied) return;
      replied = true;
      const body = JSON.stringify(json);
      res.writeHead(status, {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
      });
      res.end(body);
    };
    req.on('data', (chunk: Buffer) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reply(413, { error: 'payload too large' });
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      if (replied) return;
      const path = (req.url ?? '/').split('?')[0] ?? '/';
      void core
        .handleRequest({
          method: req.method ?? '',
          path,
          headers: req.headers,
          body: Buffer.concat(chunks).toString('utf8'),
        })
        .then((out) => reply(out.status, out.json))
        .catch(() => reply(500, { error: 'internal error' }));
    });
    req.on('error', () => reply(500, { error: 'internal error' }));
  });

  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const port = typeof address === 'object' && address ? address.port : 0;
      resolve({
        port,
        close: () =>
          new Promise<void>((done) => {
            server.closeAllConnections();
            server.close(() => done());
          }),
      });
    });
  });
}
