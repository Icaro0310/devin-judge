// Registers the vscode stub resolve hook, then runs the real argv entry.
// Usage: node --import ./test/vscode-loader-import.mjs test/smoke-activate.mjs
import { register } from 'node:module';
register('./vscode-loader.mjs', import.meta.url);
