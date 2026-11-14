/**
 * 即時 AI 權限的純函式與快取（不依賴 React、axios、import.meta），方便用 node --test 測試。
 *
 * 後端契約：
 * - GET /api/ai-access 回傳 { mode: 'all' | 'developers', live_ai: boolean }
 * - 不允許使用即時 AI 時，對話型端點回 403，detail = { code: 'LIVE_AI_RESTRICTED', message }
 *
 * /api/ai-access 只用來決定「要不要先顯示輸入框」，真正的權限檢查在後端。
 * 所以這支 API 失敗（網路錯誤、404、401、5xx、格式不符）時採 fail-open：照常顯示輸入框，
 * 若後端真的不開放，送出時會收到 403 LIVE_AI_RESTRICTED，再由 markRestricted 換成說明。
 */

export const LIVE_AI_RESTRICTED_CODE = 'LIVE_AI_RESTRICTED';
export const DEFAULT_LIVE_AI_RESTRICTED_MESSAGE = '即時 AI 問答目前只開放給開發團隊';

export type LiveAiMode = 'all' | 'developers';

/**
 * loading：還在確認（不顯示輸入框，也不顯示說明，避免閃爍）
 * allowed：可以使用即時 AI（包含無法確認時的 fail-open）
 * restricted：後端明確表示不開放（/ai-access 回 live_ai=false，或對話端點回 403 LIVE_AI_RESTRICTED）
 */
export type LiveAiStatus = 'loading' | 'allowed' | 'restricted';

export interface LiveAiAccess {
  mode: LiveAiMode;
  live_ai: boolean;
}

/** 某個使用者（key）已知的結果，存在共享快取裡。 */
export interface LiveAiCacheEntry {
  status: Exclude<LiveAiStatus, 'loading'>;
  /** false 表示 /ai-access 失敗後 fail-open 的暫定結果，下次有元件需要時會再確認一次 */
  verified: boolean;
  /** /ai-access 回傳的模式；fail-open 或 403 推得的結果沒有這個欄位 */
  mode?: LiveAiMode;
  /** 收到 403 LIVE_AI_RESTRICTED 時後端給的訊息 */
  message?: string;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null;

/** 解析 /api/ai-access 的回應；格式不符時回傳 null，由呼叫端視為無法確認。 */
export function parseAiAccessResponse(data: unknown): LiveAiAccess | null {
  if (!isRecord(data)) return null;
  const { mode, live_ai: liveAi } = data;
  if (mode !== 'all' && mode !== 'developers') return null;
  if (typeof liveAi !== 'boolean') return null;
  return { mode, live_ai: liveAi };
}

/** /ai-access 失敗或格式不符時的暫定結果：照常顯示輸入框，交給後端 403 把關。 */
export const FAIL_OPEN_ENTRY: LiveAiCacheEntry = Object.freeze({ status: 'allowed', verified: false });

/** 把 /ai-access 的解析結果轉成快取項目；null（無法確認）時 fail-open。 */
export function entryFromAiAccess(access: LiveAiAccess | null): LiveAiCacheEntry {
  if (!access) return FAIL_OPEN_ENTRY;
  return { status: access.live_ai ? 'allowed' : 'restricted', verified: true, mode: access.mode };
}

/** 使用者對應的 key：未登入統一用 'anon'。 */
export function liveAiUserKey(userId: string | null | undefined): string {
  return userId ? `user:${userId}` : 'anon';
}

/**
 * 依目前狀態算出要顯示什麼。
 * - auth 還在載入時一律 loading（還不知道是誰）。
 * - 快取裡有這位使用者的結果就直接採用（不出現骨架）。
 * - 沒有結果但已知後端是 mode=all（例如剛登入/登出/換帳號），所有人都能用，先顯示輸入框，背景再確認。
 * - 其餘情況 loading。
 */
export function deriveLiveAiStatus(args: {
  authLoading: boolean;
  entry: LiveAiCacheEntry | undefined;
  lastKnownMode: LiveAiMode | null;
}): LiveAiStatus {
  const { authLoading, entry, lastKnownMode } = args;
  if (authLoading) return 'loading';
  if (entry) return entry.status;
  if (lastKnownMode === 'all') return 'allowed';
  return 'loading';
}

export interface LiveAiAccessStore {
  /** 取得某位使用者的快取結果；同一筆結果沒變時回傳同一個物件（給 useSyncExternalStore 用）。 */
  getEntry: (key: string) => LiveAiCacheEntry | undefined;
  /** 最近一次從後端得知的模式；還不知道時是 null。 */
  getLastKnownMode: () => LiveAiMode | null;
  /** 需要時才向後端確認：已有確認過的結果、或同一個 key 正在請求中，就不重抓。 */
  ensure: (key: string) => void;
  /** 對話端點回 403 LIVE_AI_RESTRICTED 時呼叫，之後這位使用者都顯示說明。 */
  markRestricted: (key: string, message?: string) => void;
  subscribe: (listener: () => void) => () => void;
}

/**
 * 以使用者 key 為鍵的 /ai-access 共享快取。
 * 在模組層級建立一份，讓各頁面切換時沿用同一位使用者的結果，不重抓、不閃骨架；
 * 登入/登出/換帳號時 key 改變，自然會抓新的結果。
 */
export function createLiveAiAccessStore(
  fetchAccess: () => Promise<unknown>,
  warn: (...args: unknown[]) => void = (...args) => console.warn(...args),
): LiveAiAccessStore {
  const entries = new Map<string, LiveAiCacheEntry>();
  const inflight = new Set<string>();
  const listeners = new Set<() => void>();
  let lastKnownMode: LiveAiMode | null = null;

  const emit = () => {
    for (const listener of [...listeners]) listener();
  };

  const settle = (key: string, entry: LiveAiCacheEntry) => {
    inflight.delete(key);
    // 請求期間已收到 403（markRestricted），以後端實際拒絕為準，不被較早送出的結果蓋掉
    if (entries.get(key)?.status === 'restricted') return;
    entries.set(key, entry);
    if (entry.mode) lastKnownMode = entry.mode;
    emit();
  };

  return {
    getEntry: (key) => entries.get(key),
    getLastKnownMode: () => lastKnownMode,
    ensure: (key) => {
      if (inflight.has(key) || entries.get(key)?.verified) return;
      inflight.add(key);
      Promise.resolve()
        .then(fetchAccess)
        .then(
          (data) => {
            const access = parseAiAccessResponse(data);
            if (!access) warn('[live-ai] /ai-access 回應格式不符，先照常顯示輸入框（實際權限由後端把關）', data);
            return entryFromAiAccess(access);
          },
          (error: unknown) => {
            warn('[live-ai] 無法確認即時 AI 權限，先照常顯示輸入框（實際權限由後端把關）', error);
            return FAIL_OPEN_ENTRY;
          },
        )
        .then((entry) => settle(key, entry));
    },
    markRestricted: (key, message) => {
      entries.set(key, {
        status: 'restricted',
        verified: true,
        message: message?.trim() || DEFAULT_LIVE_AI_RESTRICTED_MESSAGE,
      });
      // 後端拒絕過，表示目前不是全站開放，換帳號時不要再樂觀地先顯示輸入框
      lastKnownMode = 'developers';
      emit();
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

/** 從錯誤回應的 body 辨識 LIVE_AI_RESTRICTED；是的話回傳要顯示的訊息，否則回傳 null。 */
export function liveAiRestrictedMessage(body: unknown): string | null {
  if (!isRecord(body)) return null;
  const { detail } = body;
  if (!isRecord(detail) || detail.code !== LIVE_AI_RESTRICTED_CODE) return null;
  const { message } = detail;
  return typeof message === 'string' && message.trim() ? message.trim() : DEFAULT_LIVE_AI_RESTRICTED_MESSAGE;
}

/** axios 錯誤：只有 403 且 detail.code = LIVE_AI_RESTRICTED 才算。 */
export function liveAiRestrictedMessageFromError(error: unknown): string | null {
  if (!isRecord(error) || !isRecord(error.response)) return null;
  if (error.response.status !== 403) return null;
  return liveAiRestrictedMessage(error.response.data);
}

/** fetch 回應（串流端點）：只有 403 才讀 body；body 不是 JSON 時回傳 null。 */
export async function liveAiRestrictedMessageFromResponse(res: Response): Promise<string | null> {
  if (res.status !== 403) return null;
  const body: unknown = await res.json().catch(() => null);
  return liveAiRestrictedMessage(body);
}

/**
 * 把 axios 錯誤轉成可以直接顯示的字串。
 * FastAPI 的 detail 可能是字串、物件（例如 {code, message}）或陣列（422 驗證錯誤），
 * 只有字串或物件裡的 message 字串會被採用，避免把物件直接當成 React child 渲染。
 */
export function apiErrorDisplayMessage(error: unknown, fallback: string): string {
  if (isRecord(error)) {
    const response = isRecord(error.response) ? error.response : null;
    const data = response && isRecord(response.data) ? response.data : null;
    const detail = data?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (isRecord(detail) && typeof detail.message === 'string' && detail.message.trim()) return detail.message;
    if (typeof error.message === 'string' && error.message.trim()) return error.message;
  }
  return fallback;
}
