// Minimal 'vscode' runtime stub — activation smoke tests only (test/, never shipped).
// Records every registration in `registered`; the fake model list and config
// map are mutable so tests can steer lm.selectChatModels and getConfiguration.

export const registered = {
  participants: [],
  commands: [],
  executedCommands: [],
  outputs: [],
  selectCalls: [],
  models: [],
  config: {},
};

export class EventEmitter {
  constructor() { this.listeners = []; }
  get event() { return (l) => { this.listeners.push(l); return { dispose() {} }; }; }
  fire(e) { for (const l of this.listeners) l(e); }
  dispose() {}
}

export const LanguageModelChatMessageRole = { User: 1, Assistant: 2 };
export class LanguageModelChatMessage {
  constructor(role, content) { this.role = role; this.content = content; }
  static User(content) { return new LanguageModelChatMessage(1, content); }
  static Assistant(content) { return new LanguageModelChatMessage(2, content); }
}

export class LanguageModelError extends Error {
  constructor(message, code) { super(message); this.name = 'LanguageModelError'; this.code = code; }
  static NoPermissions(message) { return new LanguageModelError(message ?? 'no permissions', 'NoPermissions'); }
  static NotFound(message) { return new LanguageModelError(message ?? 'not found', 'NotFound'); }
  static Blocked(message) { return new LanguageModelError(message ?? 'blocked', 'Blocked'); }
}

export const lm = {
  async selectChatModels(selector) {
    registered.selectCalls.push(selector ?? {});
    return registered.models;
  },
};

export const chat = {
  createChatParticipant(id, handler) {
    registered.participants.push({ id, handler });
    return { id, requestHandler: handler, dispose() {} };
  },
};

export const commands = {
  registerCommand(id, fn) { registered.commands.push({ id, fn }); return { dispose() {} }; },
  async executeCommand(id, ...args) {
    registered.executedCommands.push({ id, args });
    const c = registered.commands.find((c) => c.id === id);
    return c ? c.fn(...args) : undefined;
  },
};

export const workspace = {
  workspaceFolders: undefined,
  getConfiguration(section) {
    const values = registered.config[section] ?? {};
    return { get(key, fallback) { return key in values ? values[key] : fallback; } };
  },
};

export const window = {
  createOutputChannel(name) {
    const ch = { name, lines: [], appendLine(m) { ch.lines.push(String(m)); }, dispose() {} };
    registered.outputs.push(ch);
    return ch;
  },
  async showErrorMessage(m) { registered.errors = (registered.errors ?? []).concat(String(m)); return undefined; },
  async showInformationMessage(m) { registered.infos = (registered.infos ?? []).concat(String(m)); return undefined; },
  async showInputBox() { return undefined; },
  async showQuickPick() { return undefined; },
};

// A fake LanguageModelChat for tests/smoke: replies with `reply` (string or
// function of the request messages) streamed as one text fragment.
export function fakeChatModel(reply, fields = {}) {
  return {
    id: 'fake-1', name: 'fake-model', vendor: 'copilot', family: 'fake', version: '0',
    maxInputTokens: 4096,
    async sendRequest(messages, _options, _token) {
      const text = typeof reply === 'function' ? reply(messages) : reply;
      return { text: (async function* () { yield text; })() };
    },
    ...fields,
  };
}
