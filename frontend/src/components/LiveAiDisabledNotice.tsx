import { useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { HiLockClosed, HiSparkles } from 'react-icons/hi';
import { DEMO_CASES } from '../demo/cases';
import { DEFAULT_LIVE_AI_RESTRICTED_MESSAGE } from '../api/liveAi';
import { useAuth } from '../auth/AuthContext';
import { GoogleMark } from './GoogleLoginButton';

interface LiveAiDisabledNoticeProps {
  /** 連到示範案例頁時預先篩選的助理類型 */
  assistant?: 'course' | 'research';
  compact?: boolean;
  /** 後端 403 給的訊息，有的話取代預設標題 */
  message?: string;
}

/** 不需要呼叫對話模型、仍可照常使用的功能 */
const AVAILABLE_FEATURES = [
  { to: '/assessment', label: '興趣量表' },
  { to: '/curriculum', label: '系所修課' },
  { to: '/courses', label: '課程資訊（含語意搜尋）' },
  { to: '/projects', label: '研究計畫與 PDF' },
  { to: '/resources', label: '資源連結' },
];

const isCurrentPage = (pathname: string, to: string) => pathname === to || pathname.startsWith(`${to}/`);

/** 不能使用即時 AI 時，取代 AI 提問輸入區的說明；文案依登入狀態切換。 */
export default function LiveAiDisabledNotice({ assistant, compact = false, message }: LiveAiDisabledNoticeProps) {
  const { user, loginWithGoogle } = useAuth();
  const { pathname } = useLocation();
  const [loginError, setLoginError] = useState('');
  const isLoggedIn = !!user;
  const title = message || DEFAULT_LIVE_AI_RESTRICTED_MESSAGE;
  const description = isLoggedIn
    ? '為了控制模型費用，AI 對話暫時只開放開發團隊成員，目前登入的帳號不在開放名單中。'
    : '為了控制模型費用，AI 對話暫時只開放開發團隊成員，開發團隊成員登入後即可使用。';
  const features = AVAILABLE_FEATURES.filter((item) => !isCurrentPage(pathname, item.to));
  const demoTo = assistant ? `/demo?assistant=${assistant}` : '/demo';

  const handleLogin = async () => {
    setLoginError('');
    try {
      await loginWithGoogle();
    } catch {
      setLoginError('登入失敗，請再試一次');
    }
  };

  return (
    <div className={`shrink-0 border-t border-slate-200 ${compact ? 'p-3' : 'p-4'}`} role="status">
      <div className={`flex flex-col items-center gap-2 rounded-2xl bg-slate-50 text-center ${compact ? 'px-3 py-3' : 'px-4 py-4'}`}>
        <div className="flex h-9 w-9 items-center justify-center rounded-full bg-slate-100">
          <HiLockClosed className="h-4 w-4 text-slate-400" />
        </div>
        <p className="text-sm font-semibold text-slate-700">{title}</p>
        <p className="text-xs leading-5 text-slate-500">{description}</p>
        {!isLoggedIn && (
          <>
            <button
              type="button"
              onClick={handleLogin}
              className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-700 shadow-sm transition hover:bg-slate-50 hover:shadow"
            >
              <GoogleMark />
              使用 Google 帳號登入
            </button>
            {loginError && <p className="text-xs text-red-500">{loginError}</p>}
          </>
        )}
        {features.length > 0 && (
          <>
            <p className="text-xs leading-5 text-slate-500">以下功能不需要 AI 對話，可以照常使用：</p>
            <div className="flex flex-wrap justify-center gap-1.5">
              {features.map((item) => (
                <Link
                  key={item.to}
                  to={item.to}
                  className="rounded-full border border-slate-200 bg-white px-2.5 py-1 text-xs text-slate-600 transition hover:bg-slate-100"
                >
                  {item.label}
                </Link>
              ))}
            </div>
          </>
        )}
        {DEMO_CASES.length > 0 && (
          <Link
            to={demoTo}
            className="mt-1 inline-flex items-center gap-1.5 rounded-xl bg-primary-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-primary-800"
          >
            <HiSparkles className="h-4 w-4" />
            查看 AI 示範案例
          </Link>
        )}
      </div>
    </div>
  );
}

/** 還在確認權限時佔住輸入區的位置，不先顯示輸入框（避免出現後又消失）。 */
export function LiveAiPendingSlot({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`shrink-0 border-t border-slate-200 ${compact ? 'p-3' : 'p-4'}`} aria-busy="true">
      <div className="h-11 animate-pulse rounded-xl bg-slate-100" />
      <span className="sr-only">正在確認即時 AI 使用權限…</span>
    </div>
  );
}
