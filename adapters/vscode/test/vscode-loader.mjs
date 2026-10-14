// ESM resolve hook mapping the 'vscode' builtin (host-only) to the
// local smoke stub. Usage: node --import ./test/vscode-loader-import.mjs
export function resolve(specifier, context, next) {
  if (specifier === 'vscode') {
    return { url: new URL('./stubs/vscode.mjs', import.meta.url).href, shortCircuit: true };
  }
  return next(specifier, context);
}
