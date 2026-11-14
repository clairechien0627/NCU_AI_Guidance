// 執行：npm run test:unit（Node 內建測試，不需額外套件）
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  DEFAULT_LIVE_AI_RESTRICTED_MESSAGE,
  apiErrorDisplayMessage,
  createLiveAiAccessStore,
  deriveLiveAiStatus,
  entryFromAiAccess,
  liveAiRestrictedMessage,
  liveAiRestrictedMessageFromError,
  liveAiRestrictedMessageFromResponse,
  liveAiUserKey,
  parseAiAccessResponse,
} from '../src/api/liveAi.ts';

const restrictedBody = {
  detail: { code: 'LIVE_AI_RESTRICTED', message: '即時 AI 問答目前只開放給開發團隊' },
};

test('解析 /api/ai-access：合法回應', () => {
  assert.deepEqual(parseAiAccessResponse({ mode: 'all', live_ai: true }), { mode: 'all', live_ai: true });
  assert.deepEqual(parseAiAccessResponse({ mode: 'developers', live_ai: false }), {
    mode: 'developers',
    live_ai: false,
  });
});

test('解析 /api/ai-access：格式不符一律回 null', () => {
  for (const bad of [
    null,
    undefined,
    'all',
    {},
    { mode: 'all' },
    { live_ai: true },
    { mode: 'all', live_ai: 'true' },
    { mode: 'everyone', live_ai: true },
    '<!doctype html>',
  ]) {
    assert.equal(parseAiAccessResponse(bad), null, JSON.stringify(bad));
  }
});

test('ai-access 結果轉成快取項目：格式不符時 fail-open（照常顯示輸入框）', () => {
  assert.deepEqual(entryFromAiAccess({ mode: 'all', live_ai: true }), { status: 'allowed', verified: true, mode: 'all' });
  assert.deepEqual(entryFromAiAccess({ mode: 'developers', live_ai: true }), {
    status: 'allowed',
    verified: true,
    mode: 'developers',
  });
  assert.deepEqual(entryFromAiAccess({ mode: 'developers', live_ai: false }), {
    status: 'restricted',
    verified: true,
    mode: 'developers',
  });
  assert.deepEqual(entryFromAiAccess(null), { status: 'allowed', verified: false });
});

test('使用者 key：未登入統一是 anon', () => {
  assert.equal(liveAiUserKey(null), 'anon');
  assert.equal(liveAiUserKey(undefined), 'anon');
  assert.equal(liveAiUserKey(''), 'anon');
  assert.equal(liveAiUserKey('firebase:abc'), 'user:firebase:abc');
});

test('hook 狀態：auth 載入中一律 loading', () => {
  assert.equal(
    deriveLiveAiStatus({ authLoading: true, entry: { status: 'allowed', verified: true }, lastKnownMode: 'all' }),
    'loading',
  );
});

test('hook 狀態：快取有這位使用者的結果就直接採用（不出現骨架）', () => {
  for (const status of ['allowed', 'restricted'] as const) {
    assert.equal(
      deriveLiveAiStatus({ authLoading: false, entry: { status, verified: true }, lastKnownMode: 'developers' }),
      status,
    );
  }
  assert.equal(
    deriveLiveAiStatus({ authLoading: false, entry: { status: 'allowed', verified: false }, lastKnownMode: null }),
    'allowed',
  );
});

test('hook 狀態：沒有這位使用者的結果時，只有已知 mode=all 才先顯示輸入框', () => {
  assert.equal(deriveLiveAiStatus({ authLoading: false, entry: undefined, lastKnownMode: 'all' }), 'allowed');
  assert.equal(deriveLiveAiStatus({ authLoading: false, entry: undefined, lastKnownMode: 'developers' }), 'loading');
  assert.equal(deriveLiveAiStatus({ authLoading: false, entry: undefined, lastKnownMode: null }), 'loading');
});

/** 可控制的假 fetch：每次呼叫都回傳一個由測試決定何時 resolve/reject 的 promise。 */
function controllableFetch() {
  const calls: { resolve: (value: unknown) => void; reject: (error: unknown) => void }[] = [];
  const fetchAccess = () =>
    new Promise<unknown>((resolve, reject) => {
      calls.push({ resolve, reject });
    });
  return { calls, fetchAccess };
}

const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

function makeStore(fetchAccess: () => Promise<unknown>) {
  const warnings: unknown[][] = [];
  const store = createLiveAiAccessStore(fetchAccess, (...args) => warnings.push(args));
  let notified = 0;
  store.subscribe(() => {
    notified += 1;
  });
  return { store, warnings, notifications: () => notified };
}

test('共享快取：/ai-access 失敗（網路錯誤、404、401、5xx）時 fail-open 並 console.warn', async () => {
  const failures: unknown[] = [
    new Error('Network Error'),
    { message: 'Request failed with status code 404', response: { status: 404, data: { detail: 'Not Found' } } },
    { message: 'Request failed with status code 401', response: { status: 401, data: { detail: 'Unauthorized' } } },
    { message: 'Request failed with status code 502', response: { status: 502, data: '<html>Bad Gateway</html>' } },
  ];
  for (const failure of failures) {
    const { store, warnings, notifications } = makeStore(() => Promise.reject(failure));
    store.ensure('anon');
    await flush();
    assert.deepEqual(store.getEntry('anon'), { status: 'allowed', verified: false }, JSON.stringify(failure));
    assert.equal(warnings.length, 1);
    assert.equal(warnings[0][1], failure);
    assert.equal(notifications(), 1);
    assert.equal(store.getLastKnownMode(), null);
  }
});

test('共享快取：/ai-access 回應格式不符（例如 SPA 回 HTML）時 fail-open 並 console.warn', async () => {
  const { store, warnings } = makeStore(() => Promise.resolve('<!doctype html><html></html>'));
  store.ensure('user:a');
  await flush();
  assert.deepEqual(store.getEntry('user:a'), { status: 'allowed', verified: false });
  assert.equal(warnings.length, 1);
  assert.equal(store.getLastKnownMode(), null);
});

test('共享快取：fetchAccess 同步丟錯也會 fail-open，不會讓頁面崩潰', async () => {
  const { store, warnings } = makeStore(() => {
    throw new Error('boom');
  });
  assert.doesNotThrow(() => store.ensure('anon'));
  await flush();
  assert.deepEqual(store.getEntry('anon'), { status: 'allowed', verified: false });
  assert.equal(warnings.length, 1);
});

test('共享快取：同一身分只抓一次，各頁切換（重新 ensure）時直接沿用', async () => {
  const { calls, fetchAccess } = controllableFetch();
  const { store, warnings } = makeStore(fetchAccess);
  store.ensure('user:a');
  store.ensure('user:a'); // 同時有兩個元件掛載
  await flush();
  assert.equal(calls.length, 1);
  assert.equal(store.getEntry('user:a'), undefined);

  calls[0].resolve({ mode: 'all', live_ai: true });
  await flush();
  const entry = store.getEntry('user:a');
  assert.deepEqual(entry, { status: 'allowed', verified: true, mode: 'all' });
  assert.equal(store.getLastKnownMode(), 'all');

  store.ensure('user:a'); // 切到別頁再掛載
  await flush();
  assert.equal(calls.length, 1);
  assert.equal(store.getEntry('user:a'), entry, '同一筆結果要是同一個物件，useSyncExternalStore 才不會重繪');
  assert.equal(warnings.length, 0);
});

test('共享快取：登入/登出/換帳號（新 key）會重新抓，各自保留結果', async () => {
  const { calls, fetchAccess } = controllableFetch();
  const { store } = makeStore(fetchAccess);
  store.ensure('anon');
  await flush();
  calls[0].resolve({ mode: 'developers', live_ai: false });
  await flush();

  store.ensure('user:dev'); // 登入
  await flush();
  assert.equal(calls.length, 2);
  assert.equal(store.getEntry('user:dev'), undefined);
  calls[1].resolve({ mode: 'developers', live_ai: true });
  await flush();

  assert.equal(store.getEntry('anon')?.status, 'restricted');
  assert.equal(store.getEntry('user:dev')?.status, 'allowed');
  assert.equal(store.getLastKnownMode(), 'developers');

  store.ensure('user:other'); // 換帳號
  await flush();
  assert.equal(calls.length, 3);

  store.ensure('anon'); // 登出回到未登入：已確認過，不重抓
  await flush();
  assert.equal(calls.length, 3);
});

test('共享快取：較晚回來的舊請求只寫入自己的 key，不影響目前使用者', async () => {
  const { calls, fetchAccess } = controllableFetch();
  const { store } = makeStore(fetchAccess);
  store.ensure('anon');
  store.ensure('user:a');
  await flush();
  calls[1].resolve({ mode: 'developers', live_ai: true });
  await flush();
  calls[0].resolve({ mode: 'developers', live_ai: false });
  await flush();
  assert.equal(store.getEntry('user:a')?.status, 'allowed');
  assert.equal(store.getEntry('anon')?.status, 'restricted');
});

test('共享快取：fail-open 的暫定結果在下次 ensure 時會再確認一次', async () => {
  let attempt = 0;
  const { store } = makeStore(() => {
    attempt += 1;
    return attempt === 1
      ? Promise.reject(new Error('Network Error'))
      : Promise.resolve({ mode: 'developers', live_ai: false });
  });
  store.ensure('user:b');
  await flush();
  assert.deepEqual(store.getEntry('user:b'), { status: 'allowed', verified: false });
  store.ensure('user:b');
  await flush();
  assert.equal(attempt, 2);
  assert.deepEqual(store.getEntry('user:b'), { status: 'restricted', verified: true, mode: 'developers' });
});

test('共享快取：markRestricted 更新快取並通知訂閱者，只影響該使用者', async () => {
  const { calls, fetchAccess } = controllableFetch();
  const { store, notifications } = makeStore(fetchAccess);
  store.ensure('user:a');
  store.ensure('user:b');
  await flush();
  calls[0].resolve({ mode: 'all', live_ai: true });
  calls[1].resolve({ mode: 'all', live_ai: true });
  await flush();
  const before = notifications();

  store.markRestricted('user:a', '即時 AI 問答目前只開放給開發團隊');
  assert.equal(notifications(), before + 1);
  assert.deepEqual(store.getEntry('user:a'), {
    status: 'restricted',
    verified: true,
    message: '即時 AI 問答目前只開放給開發團隊',
  });
  assert.equal(store.getEntry('user:b')?.status, 'allowed');
  assert.equal(store.getLastKnownMode(), 'developers', '被拒絕過就不再樂觀地當成 mode=all');

  store.ensure('user:a'); // 已確認為 restricted，不重抓
  await flush();
  assert.equal(calls.length, 2);
});

test('共享快取：markRestricted 沒給訊息時用預設文案', () => {
  const { store } = makeStore(() => Promise.resolve({ mode: 'all', live_ai: true }));
  store.markRestricted('anon');
  assert.equal(store.getEntry('anon')?.message, DEFAULT_LIVE_AI_RESTRICTED_MESSAGE);
  store.markRestricted('anon', '   ');
  assert.equal(store.getEntry('anon')?.message, DEFAULT_LIVE_AI_RESTRICTED_MESSAGE);
});

test('共享快取：請求期間收到 403，較早送出的 /ai-access 結果不會把 restricted 蓋回 allowed', async () => {
  const { calls, fetchAccess } = controllableFetch();
  const { store } = makeStore(fetchAccess);
  store.ensure('user:a');
  await flush();
  store.markRestricted('user:a', '不開放');
  calls[0].resolve({ mode: 'all', live_ai: true });
  await flush();
  assert.equal(store.getEntry('user:a')?.status, 'restricted');
  assert.equal(store.getEntry('user:a')?.message, '不開放');
});

test('共享快取：取消訂閱後不再收到通知', () => {
  const store = createLiveAiAccessStore(
    () => Promise.resolve(null),
    () => {},
  );
  let count = 0;
  const unsubscribe = store.subscribe(() => {
    count += 1;
  });
  store.markRestricted('anon');
  unsubscribe();
  store.markRestricted('user:a');
  assert.equal(count, 1);
});

test('辨識 403 body：LIVE_AI_RESTRICTED 回傳後端訊息', () => {
  assert.equal(liveAiRestrictedMessage(restrictedBody), '即時 AI 問答目前只開放給開發團隊');
});

test('辨識 403 body：沒有 message 時用預設文案', () => {
  assert.equal(
    liveAiRestrictedMessage({ detail: { code: 'LIVE_AI_RESTRICTED' } }),
    DEFAULT_LIVE_AI_RESTRICTED_MESSAGE,
  );
  assert.equal(
    liveAiRestrictedMessage({ detail: { code: 'LIVE_AI_RESTRICTED', message: '   ' } }),
    DEFAULT_LIVE_AI_RESTRICTED_MESSAGE,
  );
});

test('辨識 403 body：其他錯誤不算', () => {
  for (const body of [
    null,
    'Forbidden',
    { detail: '開發人員專用功能' },
    { detail: { code: 'OTHER', message: 'x' } },
    { detail: [{ msg: 'field required' }] },
    { code: 'LIVE_AI_RESTRICTED', detail: '舊版頂層 code 格式' },
  ]) {
    assert.equal(liveAiRestrictedMessage(body), null, JSON.stringify(body));
  }
});

test('axios 錯誤：只有 403 + LIVE_AI_RESTRICTED 才算', () => {
  assert.equal(
    liveAiRestrictedMessageFromError({ response: { status: 403, data: restrictedBody } }),
    '即時 AI 問答目前只開放給開發團隊',
  );
  assert.equal(liveAiRestrictedMessageFromError({ response: { status: 401, data: restrictedBody } }), null);
  assert.equal(
    liveAiRestrictedMessageFromError({ response: { status: 403, data: { detail: '開發人員專用功能' } } }),
    null,
  );
  assert.equal(liveAiRestrictedMessageFromError(new Error('Network Error')), null);
  assert.equal(liveAiRestrictedMessageFromError(undefined), null);
});

test('fetch 回應（串流端點）：403 JSON 才辨識，非 JSON 或其他狀態回 null', async () => {
  const json = (status: number, body: unknown) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

  assert.equal(await liveAiRestrictedMessageFromResponse(json(403, restrictedBody)), '即時 AI 問答目前只開放給開發團隊');
  assert.equal(await liveAiRestrictedMessageFromResponse(json(401, restrictedBody)), null);
  assert.equal(await liveAiRestrictedMessageFromResponse(json(403, { detail: 'Forbidden' })), null);
  assert.equal(await liveAiRestrictedMessageFromResponse(new Response('<html>Forbidden</html>', { status: 403 })), null);
});

test('錯誤訊息轉字串：物件或陣列 detail 不會原樣傳出去', () => {
  const fallback = '稍後再試';
  assert.equal(apiErrorDisplayMessage({ response: { data: { detail: '找不到課程' } } }, fallback), '找不到課程');
  assert.equal(apiErrorDisplayMessage({ response: { data: restrictedBody } }, fallback), '即時 AI 問答目前只開放給開發團隊');
  assert.equal(
    apiErrorDisplayMessage({ response: { data: { detail: [{ msg: 'field required' }] } }, message: 'Request failed with status code 422' }, fallback),
    'Request failed with status code 422',
  );
  assert.equal(apiErrorDisplayMessage({ response: { data: { detail: [{ msg: 'x' }] } } }, fallback), fallback);
  assert.equal(apiErrorDisplayMessage(null, fallback), fallback);
  assert.equal(apiErrorDisplayMessage('oops', fallback), fallback);
});
