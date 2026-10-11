const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

// Execute the real browser functions with deterministic network/DOM boundaries.
// No browser, credentials, upstream traffic, build step or third-party modules.
const root = path.resolve(__dirname, '..');
const app = fs.readFileSync(path.join(root, 'bbw_web/static/app.js'), 'utf8');
const admin = fs.readFileSync(path.join(root, 'bbw_web/static/admin.js'), 'utf8');
function fn(source, name) {
  const match = new RegExp(`^(?:async )?function ${name}\\(`, 'm').exec(source);
  assert.ok(match, `Missing function ${name}`);
  const tail = source.slice(match.index);
  const end = /^}/m.exec(tail);
  assert.ok(end, `Missing closing brace for ${name}`);
  return tail.slice(0, end.index + 1);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function response(status, body = '{}') {
  return { status, ok: status >= 200 && status < 300, headers: new Headers(), text: async () => body };
}
function appContext(fetch) {
  const state = { authenticated: true, sessionGeneration: 1, user: { uid: 'current-user' } };
  const context = {
    S: state, fetch, AbortController, DOMException, FormData, navigator: { onLine: true },
    setTimeout, clearTimeout, performanceClockNow: () => 0,
    performanceMetricPath: (value) => value, serverTimingAppDuration: () => null,
    roundedPerformanceMs: (value) => value, recordPerformanceMetric: () => {},
    VOICE_MATCH_ENABLED: false, cleanupIM: async () => {}, clearSensitiveBrowserStorage: async () => {},
  };
  for (const name of [
    'stopMatchStatusPolling', 'setAiAgentAccess', 'rememberVisiblePresenceTab', 'clearAllViewCaches',
    'resetMessageSearchState', 'stopPresenceTimer', 'stopMessageSyncTimer', 'closeMessageSyncChannel',
    'disconnectMomentViewTracking', 'clearPeerMediaReconcile', 'clearMessageArchiveDeliveryState',
    'persistPendingFlashAcknowledgements', 'clearFlashAckDeliveryState', 'scrubAuthenticatedDom',
    'applyUser', 'showLogin', 'toast',
  ]) context[name] = () => {};
  vm.createContext(context);
  vm.runInContext(app.slice(app.indexOf('class AuthExpiredError'), app.indexOf('function archiveHash')), context);
  return context;
}
function adminContext(fetch) {
  const state = { authenticated: true, authGeneration: 1, requestControllers: new Set() };
  const context = {
    ADMIN_STATE: state, fetch, AbortController, FormData, setTimeout, clearTimeout,
    errorMessage: () => 'Request failed',
    handleAuthenticationExpired: () => { state.authenticated = false; state.authGeneration++; },
  };
  vm.createContext(context);
  vm.runInContext(admin.slice(admin.indexOf('class AdminApiError'), admin.indexOf('function element(')) + '\n' + fn(admin, 'adminApi'), context);
  return context;
}

for (const [label, create, call, generation] of [
  ['app', appContext, 'api', 'sessionGeneration'],
  ['admin', adminContext, 'adminApi', 'authGeneration'],
]) {
  test(`${label}: non-JSON 401 expires the current session`, async () => {
    const context = create(async () => response(401, '<html>Unauthorized</html>'));
    const state = context.S || context.ADMIN_STATE;
    await assert.rejects(context[call]('/api/private'), (error) => {
      assert.match(error.message, /登录已失效/);
      return true;
    });
    assert.equal(state.authenticated, false);
  });
  test(`${label}: late 401 cannot log out a newer session`, async () => {
    const pending = deferred();
    const context = create(() => pending.promise);
    const state = context.S || context.ADMIN_STATE;
    const request = context[call]('/api/private');
    state[generation]++;
    pending.resolve(response(401));
    await assert.rejects(request);
    assert.equal(state.authenticated, true);
    assert.equal(state[generation], 2);
  });
  test(`${label}: successful old-account body is rejected after account switch`, async () => {
    const body = deferred();
    const entered = deferred();
    const context = create(async () => ({ ...response(200), text: () => { entered.resolve(); return body.promise; } }));
    const state = context.S || context.ADMIN_STATE;
    const request = context[call]('/api/private');
    await entered.promise;
    state[generation]++;
    body.resolve('{"secret":"old-account-data"}');
    await assert.rejects(request);
    assert.equal(state.authenticated, true);
  });
  test(`${label}: valid success remains usable and malformed 200 remains an error`, async () => {
    const context = create(async () => response(200, '{"ok":true,"value":7}'));
    const result = await context[call]('/api/private');
    assert.equal((result.data || result).value, 7);
    context.fetch = async () => response(200, '<html>broken</html>');
    await assert.rejects(context[call]('/api/private'), /无法识别/);
  });
}

test('admin: optional unauthorized bootstrap returns null even for an HTML body', async () => {
  const context = adminContext(async () => response(401, '<html>Unauthorized</html>'));
  assert.equal(await context.adminApi('/api/admin/me', { authOptional: true }), null);
});

function listContext() {
  const pending = [];
  const nodes = new Map();
  const state = { authenticated: true, pageHidden: false, viewGeneration: 1, listRequestGenerations: {}, userPage: 1, invitePage: 1, auditPage: 1 };
  const context = {
    ADMIN_STATE: state, ADMIN_PAGE_LIMIT: 25,
    ADMIN_ENDPOINTS: { users: '/users', invites: '/invites', audits: '/audits' },
    URLSearchParams,
    FormData: class { constructor(form) { this.form = form; } get(key) { return this.form[key] || ''; } },
    $: (id) => {
      if (!nodes.has(id)) nodes.set(id, { value: '', textContent: '', replaceChildren() {}, rendered: null });
      return nodes.get(id);
    },
    adminApi: (url) => { const item = deferred(); pending.push({ ...item, url }); return item.promise; },
    renderLoading: (node) => { node.rendered = 'loading'; },
    renderTable: (node, columns, rows) => { node.rendered = rows; },
    renderError: (node, error) => { node.rendered = error.message; },
    renderPagination: (node, meta) => { node.rendered = meta; },
    resultSummaryText: (meta, count, filters) => filters,
    extractItems: (data) => data.items,
    paginationMeta: (data, page) => ({ page, total: 1 }),
    markPageUpdated: () => {},
  };
  vm.createContext(context);
  const helpers = ['beginAdminListRequest', 'isCurrentAdminListRequest'];
  const code = helpers.filter((name) => admin.includes(`function ${name}(`)).map((name) => fn(admin, name));
  vm.runInContext([...code, ...['queryString', 'loadUsers', 'loadInvites', 'loadAudits'].map((name) => fn(admin, name))].join('\n'), context);
  return { context, state, pending, nodes };
}
for (const [method, tableId, paginationId] of [
  ['loadUsers', 'admin-user-table', 'admin-user-pagination'],
  ['loadInvites', 'admin-invite-table', 'admin-invite-pagination'],
  ['loadAudits', 'admin-audit-table', 'admin-audit-pagination'],
]) {
  for (const lateFailure of [false, true]) {
    test(`admin ${method}: old ${lateFailure ? 'error' : 'result'} cannot overwrite newer page`, async () => {
      const { context, pending, nodes } = listContext();
      const old = context[method](1);
      const current = context[method](2);
      pending[1].resolve({ items: [{ id: 'current' }] });
      await current;
      if (lateFailure) pending[0].reject(new Error('stale error'));
      else pending[0].resolve({ items: [{ id: 'old' }] });
      await old;
      assert.equal(nodes.get(tableId).rendered[0].id, 'current');
      assert.equal(nodes.get(paginationId).rendered.page, 2);
    });
  }
}

test('SDK receipts: switching account stops subsequent batches and cannot contaminate new receipt state', async () => {
  const firstBatch = deferred();
  let newSdkCalls = 0;
  const state = {
    authenticated: true, sessionGeneration: 1, imMode: 'sdk',
    chat: { sendMessageReadReceipt: () => firstBatch.promise },
    imMessages: Array.from({ length: 35 }, (_, index) => ({ id: `m${index}`, peer: 'peer', rawMessage: { id: index } })),
    sdkMessageReadReceiptPending: new Set(), sdkMessageReadReceiptReported: new Set(),
  };
  const context = {
    S: state, console, isVisibleActiveConversation: () => true,
    sdkMessageNeedsReadReceipt: () => true, messageIdentityKey: (entry) => entry.id,
    withTimeout: (promise) => promise,
  };
  vm.createContext(context);
  vm.runInContext(fn(app, 'isCurrentAuthenticatedSession') + '\n' + fn(app, 'reportSdkMessageReadReceipts'), context);
  const request = context.reportSdkMessageReadReceipts('peer');
  assert.equal(state.sdkMessageReadReceiptPending.size, 30);
  state.sessionGeneration++;
  state.chat = { sendMessageReadReceipt: async () => { newSdkCalls++; } };
  state.sdkMessageReadReceiptPending.clear();
  state.sdkMessageReadReceiptPending.add('m0');
  firstBatch.resolve();
  assert.equal(await request, false);
  assert.equal(newSdkCalls, 0);
  assert.equal(state.sdkMessageReadReceiptReported.size, 0);
  assert.equal(state.sdkMessageReadReceiptPending.has('m0'), true);
});

test('app: optional/credential 401 and permission 403 do not erase a valid session', async () => {
  const context = appContext(async () => response(401, '{"ok":false,"error":"bad credentials"}'));
  const optional = await context.api('/api/me', { authOptional: true });
  const credential = await context.api('/api/auth/login');
  assert.equal(optional.status, 401);
  assert.equal(credential.data.error, 'bad credentials');
  context.fetch = async () => response(403, '{"ok":false,"error":"access denied"}');
  assert.equal((await context.api('/api/private')).status, 403);
  assert.equal(context.S.authenticated, true);
  assert.equal(context.S.sessionGeneration, 1);
});

test('app: delayed expired-session cleanup must not erase a newly logged-in account', async () => {
  const cleanup = deferred();
  const context = appContext(async () => response(401));
  let storageClears = 0;
  context.cleanupIM = () => cleanup.promise;
  context.clearSensitiveBrowserStorage = async () => { storageClears++; };
  await assert.rejects(context.api('/api/private'));
  context.S.authenticated = true;
  context.S.sessionGeneration++;
  cleanup.resolve();
  await cleanup.promise;
  await Promise.resolve();
  assert.equal(storageClears, 0);
});

for (const switchSession of [false, true]) {
  test(`conversation read rollback ${switchSession ? 'does not affect a newer account with reused version' : 'restores unread on a current-session failure'}`, async () => {
    const report = deferred();
    let timerCallback;
    let recalculated = 0;
    const state = {
      authenticated: true, sessionGeneration: 1, conversationReadReportTimers: new Map(),
      conversationReadReportVersions: new Map(), readConversationPeers: new Map([['peer', 0]]),
      conversations: [{ peer: 'peer', unread_count: 0 }],
    };
    const context = {
      S: state, setTimeout: (callback) => { timerCallback = callback; return 1; }, clearTimeout: () => {},
      reportConversationRead: () => report.promise,
      conversationPeer: (value) => value.peer, conversationUnreadCount: (value) => value.unread_count,
      recalculateUnreadTotal: () => { recalculated++; }, isVisibleActiveConversation: () => false,
    };
    vm.createContext(context);
    vm.runInContext(fn(app, 'isCurrentAuthenticatedSession') + '\n' + fn(app, 'scheduleConversationReadReport'), context);
    context.scheduleConversationReadReport('peer', 400, 3);
    const inflight = timerCallback();
    if (switchSession) {
      state.sessionGeneration++;
      state.conversationReadReportVersions.clear();
      state.conversationReadReportVersions.set('peer', 1);
      state.conversations = [{ peer: 'peer', unread_count: 0 }];
    }
    report.resolve(false);
    await inflight;
    assert.equal(state.conversations[0].unread_count, switchSession ? 0 : 3);
    assert.equal(recalculated, switchSession ? 0 : 1);
  });
}

function exportContext(fetch, values = []) {
  const blobs = [];
  const downloads = [];
  const context = {
    S: { authenticated: true, sessionGeneration: 1, activePeer: 'peer', user: { uid: 'self' } },
    fetch, AbortController, DOMException, Blob, setTimeout, clearTimeout,
    peerChatMessageEntries: () => values.map((text) => ({ text, sender_name: text, kind: 'text' })),
    chatMessageTimeInfo: () => ({ datetime: '2026-10-11' }), toast: () => {},
    URL: { createObjectURL: (blob) => { blobs.push(blob); return 'blob:test'; }, revokeObjectURL: () => {} },
    document: { body: { appendChild() {} }, createElement: () => ({ click() { downloads.push(this); }, remove() {} }) },
  };
  vm.createContext(context);
  vm.runInContext(fn(app, 'isCurrentAuthenticatedSession') + '\n' + fn(app, 'exportActiveChatRecord'), context);
  return { context, blobs, downloads };
}

test('CSV fallback: neutralizes spreadsheet formulas in message and sender without corrupting ordinary text', async () => {
  const values = ['=1+1', '+SUM(1,2)', '-1+2', '@SUM(1,2)', '  =1+1', '\t=1+1', '\r=1+1', '正常, "内容"', 'line1\rline2'];
  const { context, blobs } = exportContext(async () => response(503), values);
  await context.exportActiveChatRecord();
  const csv = await blobs[0].text();
  const lines = csv.replace(/^\uFEFF/, '').split('\r\n');
  for (let index = 0; index < 7; index++) {
    assert.match(lines[index + 1], /^"2026-10-11","'/);
    assert.match(lines[index + 1], /,"text","'/);
  }
  assert.match(lines[8], /"正常, ""内容"""/);
  assert.match(lines[9], /line1\\nline2/);
});

test('CSV server export: delayed body cannot download previous-account data after logout', async () => {
  const body = deferred();
  const entered = deferred();
  const { context, downloads } = exportContext(async () => ({
    ...response(200), headers: new Headers({ 'content-type': 'text/csv' }),
    blob: () => { entered.resolve(); return body.promise; },
  }));
  const request = context.exportActiveChatRecord();
  await entered.promise;
  context.S.sessionGeneration++;
  body.resolve(new Blob(['old,account,data']));
  await assert.rejects(request, { name: 'AbortError' });
  assert.equal(downloads.length, 0);
});

for (const [label, create, call, generation] of [
  ['app', appContext, 'api', 'sessionGeneration'],
  ['admin', adminContext, 'adminApi', 'authGeneration'],
]) {
  test(`${label}: stale network failure is classified as session cancellation, not a new-account error`, async () => {
    const pending = deferred();
    const context = create(() => pending.promise);
    const state = context.S || context.ADMIN_STATE;
    const request = context[call]('/api/private');
    state[generation]++;
    pending.reject(new Error('stale network failure'));
    await assert.rejects(request, (error) => {
      if (label === 'app') assert.equal(error.name, 'AbortError');
      else assert.match(error.message, /会话已变更/);
      return true;
    });
    assert.equal(state.authenticated, true);
  });
}

test('SDK receipts: a current visible conversation sends every batch once', async () => {
  const batches = [];
  const state = {
    authenticated: true, sessionGeneration: 1, imMode: 'sdk',
    chat: { sendMessageReadReceipt: async (batch) => { batches.push(batch); } },
    imMessages: Array.from({ length: 35 }, (_, index) => ({ id: `m${index}`, peer: 'peer', rawMessage: { id: index } })),
    sdkMessageReadReceiptPending: new Set(), sdkMessageReadReceiptReported: new Set(),
  };
  const context = {
    S: state, console, isVisibleActiveConversation: () => true,
    sdkMessageNeedsReadReceipt: () => true, messageIdentityKey: (entry) => entry.id,
    withTimeout: (promise) => promise,
  };
  vm.createContext(context);
  vm.runInContext(fn(app, 'isCurrentAuthenticatedSession') + '\n' + fn(app, 'reportSdkMessageReadReceipts'), context);
  assert.equal(await context.reportSdkMessageReadReceipts('peer'), true);
  assert.equal(batches.length, 2);
  assert.equal(batches[0].length, 30);
  assert.equal(batches[1].length, 5);
  assert.equal(state.sdkMessageReadReceiptPending.size, 0);
  assert.equal(state.sdkMessageReadReceiptReported.size, 35);
  assert.equal(await context.reportSdkMessageReadReceipts('peer'), true);
  assert.equal(batches.length, 2);
});

function flashStorageContext() {
  const values = new Map();
  const preferences = [];
  let handler;
  let drainCalls = 0;
  const context = {
    S: {
      authenticated: true, user: { uid: 'account-a' }, showAllConversations: false,
      flashAckAccount: 'account-a', flashAckPending: new Map(), flashAckDrainTimer: null,
    },
    FLASH_ACK_STORAGE_PREFIX: 'bbw:im:flash-acks:',
    CONVERSATION_SHOW_ALL_STORAGE_PREFIX: 'bbw:im:show-all-conversations:',
    FLASH_ACK_LOCAL_RETENTION_MS: 10 * 60 * 1000,
    clearTimeout,
    localStorage: {
      getItem: (key) => values.get(key) || null,
      setItem: (key, value) => values.set(key, value),
      removeItem: (key) => values.delete(key),
    },
    window: { addEventListener: (event, callback) => { assert.equal(event, 'storage'); handler = callback; } },
    scheduleFlashAckDrain: () => { drainCalls++; },
    setShowAllConversations: async (enabled, options) => { preferences.push({ enabled, options }); },
  };
  vm.createContext(context);
  const functions = [
    'archiveHash', 'messageSyncAccountId', 'conversationPreferenceStorageKey', 'flashAckStorageKey',
    'normalizeStoredFlashAcknowledgements', 'persistPendingFlashAcknowledgements', 'syncFlashAckAccount',
  ].map((name) => fn(app, name));
  const start = app.indexOf('window.addEventListener("storage",');
  assert.ok(start >= 0);
  const end = app.indexOf('\ndocument.addEventListener("visibilitychange",', start);
  assert.ok(end > start);
  vm.runInContext(functions.join('\n') + '\n' + app.slice(start, end), context);
  return { context, values, preferences, emit: (event) => handler(event), drains: () => drainCalls };
}

test('flash storage event: same-account acknowledgement merges without discarding memory-only retries', () => {
  const { context, values, emit, drains } = flashStorageContext();
  const now = Date.now();
  context.S.flashAckPending.set('memory-only', { id: 'memory-only', viewedAt: now });
  const key = context.flashAckStorageKey();
  values.set(key, JSON.stringify({ items: [{ id: 'other-tab-photo', viewedAt: now }] }));
  emit({ key });
  assert.equal(context.S.flashAckPending.has('other-tab-photo'), true);
  assert.equal(context.S.flashAckPending.has('memory-only'), true);
  assert.equal(drains(), 1);
  const persisted = JSON.parse(values.get(key)).items.map((item) => item.id).sort();
  assert.deepEqual(persisted, ['memory-only', 'other-tab-photo']);
});

test('flash storage event: other accounts and unrelated keys cannot enqueue acknowledgements', () => {
  const { context, values, emit, drains } = flashStorageContext();
  const otherKey = context.flashAckStorageKey('account-b');
  values.set(otherKey, JSON.stringify({ items: [{ id: 'private-other-account', viewedAt: Date.now() }] }));
  emit({ key: otherKey });
  emit({ key: 'unrelated-key' });
  assert.equal(context.S.flashAckPending.size, 0);
  assert.equal(drains(), 0);
});

test('flash storage event: storage clear and logged-out events are ignored safely', () => {
  const { context, emit, drains } = flashStorageContext();
  assert.doesNotThrow(() => emit({ key: null }));
  context.S.authenticated = false;
  emit({ key: context.flashAckStorageKey() });
  assert.equal(drains(), 0);
});

test('flash storage event: account switch ignores prior account then restores only the current account', () => {
  const { context, values, emit, drains } = flashStorageContext();
  const oldKey = context.flashAckStorageKey();
  context.S.flashAckPending.set('old-account', { id: 'old-account', viewedAt: Date.now() });
  context.S.user = { uid: 'account-b' };
  emit({ key: oldKey });
  assert.equal(drains(), 0);
  const currentKey = context.flashAckStorageKey();
  values.set(currentKey, JSON.stringify({ items: [{ id: 'new-account', viewedAt: Date.now() }] }));
  emit({ key: currentKey });
  assert.equal(context.S.flashAckAccount, 'account-b');
  assert.deepEqual([...context.S.flashAckPending.keys()], ['new-account']);
  assert.equal(drains(), 1);
});

test('storage event: conversation preference uses its separate account key without flashing an ACK', () => {
  const { context, preferences, emit, drains } = flashStorageContext();
  emit({ key: context.conversationPreferenceStorageKey(), newValue: 'true' });
  emit({ key: context.conversationPreferenceStorageKey('account-b'), newValue: 'true' });
  assert.equal(preferences.length, 1);
  assert.equal(preferences[0].enabled, true);
  assert.equal(preferences[0].options.persistRemote, false);
  assert.equal(preferences[0].options.notify, false);
  assert.equal(drains(), 0);
});
