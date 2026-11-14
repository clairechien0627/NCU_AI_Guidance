import { useCallback, useEffect, useSyncExternalStore } from 'react';
import { useAuth } from '../auth/AuthContext';
import { liveAiAPI } from '../api/services';
import { createLiveAiAccessStore, deriveLiveAiStatus, liveAiUserKey } from '../api/liveAi';
import type { LiveAiStatus } from '../api/liveAi';

export interface LiveAiAccessState {
  status: LiveAiStatus;
  /** 只有可以顯示輸入框時才是 true（包含 /ai-access 失敗時的 fail-open）；載入中、不開放都是 false */
  allowed: boolean;
  loading: boolean;
  /** 收到 403 LIVE_AI_RESTRICTED 時後端給的訊息 */
  message?: string;
  /** 請求被後端以 LIVE_AI_RESTRICTED 拒絕時呼叫，讓輸入區立刻換成說明（同一位使用者的各頁面都會套用） */
  markRestricted: (message?: string) => void;
}

/** 模組層級的共享快取：同一位使用者在各頁面之間切換時不重抓 /ai-access。 */
const liveAiAccessStore = createLiveAiAccessStore(() => liveAiAPI.getAccess());

/**
 * 向後端 GET /api/ai-access 確認目前登入者能不能使用即時 AI（對話型 LLM）。
 * - 以後端為準，不讀任何 VITE 開關；結果以使用者為鍵快取，登入/登出/換帳號時用新鍵重新確認
 *   （apiClient 會自動帶 Firebase token）。
 * - /ai-access 失敗或格式不符時 fail-open（照常顯示輸入框，console.warn 記錄），
 *   真的不開放時由對話端點的 403 LIVE_AI_RESTRICTED + markRestricted 換成說明。
 */
export function useLiveAiAccess(): LiveAiAccessState {
  const { user, loading: authLoading } = useAuth();
  const userKey = liveAiUserKey(user?.id);
  const entry = useSyncExternalStore(liveAiAccessStore.subscribe, () => liveAiAccessStore.getEntry(userKey));
  const lastKnownMode = useSyncExternalStore(liveAiAccessStore.subscribe, liveAiAccessStore.getLastKnownMode);

  useEffect(() => {
    if (authLoading) return;
    liveAiAccessStore.ensure(userKey);
  }, [authLoading, userKey]);

  const markRestricted = useCallback(
    (message?: string) => liveAiAccessStore.markRestricted(userKey, message),
    [userKey],
  );

  const status = deriveLiveAiStatus({ authLoading, entry, lastKnownMode });

  return {
    status,
    allowed: status === 'allowed',
    loading: status === 'loading',
    message: status === 'restricted' ? entry?.message : undefined,
    markRestricted,
  };
}
