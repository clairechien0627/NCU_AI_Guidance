import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { HiChevronDown, HiUser, HiLogout, HiChartBar } from 'react-icons/hi';
import { useAuth } from '../auth/AuthContext';
import { isDeveloper } from '../auth/developerUtils';

export function GoogleMark() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="h-4 w-4 shrink-0">
      <path fill="#4285F4" d="M21.6 12.2c0-.7-.1-1.3-.2-1.9H12v3.6h5.4c-.2 1.2-.9 2.3-2 3v2.4h3.2c1.9-1.7 3-4.2 3-7.1z" />
      <path fill="#34A853" d="M12 22c2.7 0 5-.9 6.6-2.5l-3.2-2.4c-.9.6-2 1-3.4 1-2.6 0-4.8-1.8-5.6-4.1H3.1v2.5C4.8 19.8 8.1 22 12 22z" />
      <path fill="#FBBC05" d="M6.4 14c-.2-.6-.3-1.3-.3-2s.1-1.4.3-2V7.5H3.1C2.4 8.9 2 10.4 2 12s.4 3.1 1.1 4.5L6.4 14z" />
      <path fill="#EA4335" d="M12 5.9c1.5 0 2.8.5 3.8 1.5l2.8-2.8C16.9 3 14.7 2 12 2 8.1 2 4.8 4.2 3.1 7.5L6.4 10c.8-2.3 3-4.1 5.6-4.1z" />
    </svg>
  );
}

function Avatar({ picture, name }: { picture: string; name: string }) {
  if (picture) {
    return (
      <img
        src={picture}
        alt={name}
        className="h-7 w-7 shrink-0 rounded-full object-cover ring-2 ring-white"
      />
    );
  }
  return (
    <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary-600 text-xs font-bold text-white ring-2 ring-white">
      {name?.[0]?.toUpperCase() ?? 'U'}
    </div>
  );
}

export default function GoogleLoginButton() {
  const { user, loading, loginWithGoogle, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState('');
  const menuRef = useRef<HTMLDivElement>(null);
  const isDev = isDeveloper(user?.email);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const handleLogin = async () => {
    setError('');
    try {
      await loginWithGoogle();
    } catch {
      setError('登入失敗，請再試一次');
    }
  };

  if (loading) {
    return (
      <div className="flex h-9 w-28 animate-pulse items-center gap-2 rounded-xl border border-slate-200 bg-slate-100 px-3" />
    );
  }

  if (!user) {
    return (
      <button
        type="button"
        onClick={handleLogin}
        className="nav-chip inline-flex items-center gap-1.5"
        title={error || undefined}
      >
        <GoogleMark />
        <span>使用 Google 登入</span>
      </button>
    );
  }

  return (
    <div ref={menuRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`flex items-center gap-2 rounded-xl border px-3 py-1.5 text-sm font-medium transition-all ${
          open
            ? 'border-primary-300 bg-primary-50 text-primary-800 shadow-sm'
            : 'border-slate-200 bg-white text-slate-700 shadow-sm hover:border-primary-200 hover:bg-primary-50 hover:text-primary-800'
        }`}
      >
        <Avatar picture={user.picture} name={user.name} />
        <span className="max-w-[96px] truncate">{user.name}</span>
        <HiChevronDown
          className={`h-3.5 w-3.5 shrink-0 text-slate-400 transition-transform duration-150 ${
            open ? 'rotate-180' : ''
          }`}
        />
      </button>

      {open && (
        <div className="absolute right-0 top-full z-50 mt-2 w-52 overflow-hidden rounded-2xl border border-slate-100 bg-white shadow-xl">
          {/* 使用者資訊 */}
          <div className="flex items-center gap-3 border-b border-slate-100 px-4 py-3">
            <Avatar picture={user.picture} name={user.name} />
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold text-slate-800">{user.name}</p>
              <p className="truncate text-xs text-slate-400">{user.email}</p>
            </div>
          </div>

          {/* 選單項目 */}
          <div className="py-1">
            <Link
              to="/profile"
              onClick={() => setOpen(false)}
              className="flex items-center gap-3 px-4 py-2.5 text-sm text-slate-700 hover:bg-slate-50"
            >
              <HiUser className="h-4 w-4 text-slate-400" />
              個人分析
            </Link>
            {isDev && (
              <Link
                to="/monitor"
                onClick={() => setOpen(false)}
                className="flex items-center gap-3 px-4 py-2.5 text-sm text-slate-700 hover:bg-slate-50"
              >
                <HiChartBar className="h-4 w-4 text-slate-400" />
                <span>系統監控</span>
                <span className="ml-auto rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700">
                  Dev
                </span>
              </Link>
            )}
          </div>

          <div className="border-t border-slate-100 py-1">
            <button
              type="button"
              onClick={() => {
                logout();
                setOpen(false);
              }}
              className="flex w-full items-center gap-3 px-4 py-2.5 text-sm text-red-500 hover:bg-red-50"
            >
              <HiLogout className="h-4 w-4" />
              登出
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
