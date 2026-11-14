import { useMemo, useRef, useEffect, useState, useCallback } from 'react';
import {
  HiBookOpen,
  HiChat,
  HiChevronLeft,
  HiChevronRight,
  HiLockClosed,
  HiMenu,
  HiPaperAirplane,
  HiPencil,
  HiPlus,
  HiStop,
  HiTrash,
} from 'react-icons/hi';
import { chatAPI, chatStreamAPI } from '../api/services';
import CourseSidePanel from '../components/CourseSidePanel';
import CourseDetailModal from '../components/CourseDetailModal';
import DebugTracePanel from '../components/DebugTracePanel';
import HighlightedAnswer from '../components/HighlightedAnswer';
import LiveAiDisabledNotice, { LiveAiPendingSlot } from '../components/LiveAiDisabledNotice';
import { useAuth } from '../auth/AuthContext';
import { useLiveAiAccess } from '../hooks/useLiveAiAccess';
import type { CourseCard, DebugTrace, StreamEvent, ToolTraceItem } from '../types';

const TOOL_LABELS: Record<string, string> = {
  search_courses:              '搜尋課程',
  get_dept_courses:            '查詢系所課程',
  get_program_courses:         '查詢學程課程',
  get_teacher_info:            '查詢教師資訊',
  search_teachers:             '搜尋教師',
  get_graduation_rules:        '查詢畢業規定',
  get_dept_info:               '查詢系所介紹',
  get_program_description:     '查詢學程說明',
  get_requirements_notes:      '查詢修業規定',
  find_similar_courses:        '搜尋相似課程',
  get_course_knowledge_map:    '查詢知識地圖',
  get_depts_by_tech:           '查詢技術系所',
  ppr_explore:                 '知識圖譜探索',
  search_programs:             '搜尋學分學程',
  get_graduation_requirements: '查詢畢業規定',
  get_course_detail:           '查詢課程詳情',
  explore_concept_neighborhood: '探索概念鄰域',
};

interface ToolIndicator {
  name: string;
  done: boolean;
  count?: number;
}

interface Message {
  role:         'user' | 'assistant';
  content:      string;
  timestamp:    Date;
  isStreaming?: boolean;
  courseCards?: CourseCard[];
  toolsUsed?:  string[];
  debugTrace?: DebugTrace;
}

interface Conversation {
  id:        string;
  title:     string;
  messages:  Message[];
  createdAt: Date;
  updatedAt: Date;
  isStub?:   boolean; // true = metadata only, full turns not yet loaded
}

const GREETING = '你好，我是課程推薦助理。你可以直接問我課程方向、學分安排，或想比較的學院特色。';

function makeConvFromSession(detail: {
  session_id: string;
  title: string;
  updated_at: string;
  turns: Array<{
    user: string; assistant: string;
    course_cards: CourseCard[];
    tools_used: string[]; created_at: string;
    debug_trace?: { toolCalls: import('../types').ToolTraceItem[] };
  }>;
}): Conversation {
  const messages: Message[] = [
    { role: 'assistant', content: GREETING, timestamp: new Date() },
  ];
  for (const t of detail.turns) {
    const ts = new Date(t.created_at);
    messages.push({ role: 'user', content: t.user, timestamp: ts });
    const dt = t.debug_trace;
    const debugTrace: DebugTrace | undefined = dt?.toolCalls?.length
      ? { toolCalls: dt.toolCalls, verify: null }
      : undefined;
    messages.push({
      role: 'assistant', content: t.assistant, timestamp: ts,
      courseCards: t.course_cards ?? [],
      toolsUsed:   t.tools_used  ?? [],
      debugTrace,
    });
  }
  const updatedAt = new Date(detail.updated_at || Date.now());
  return {
    id: detail.session_id,
    title: detail.title || '未命名對話',
    messages,
    createdAt: updatedAt,
    updatedAt,
    isStub: false,
  };
}

function formatTime(date: Date): string {
  const diff = Date.now() - date.getTime();
  const days = Math.floor(diff / 86400000);
  if (days === 0) return '今天';
  if (days === 1) return '昨天';
  if (days < 7) return `${days} 天前`;
  return date.toLocaleDateString('zh-TW', { month: 'short', day: 'numeric' });
}

type ConvListProps = {
  conversations: Conversation[];
  selectedConversationId: string;
  onSelectConversation: (id: string) => void;
  onDeleteConversation: (id: string) => void;
  onRenameConversation: (id: string, title: string) => void;
  onSelect?: () => void;
};

function ConvList({ conversations, selectedConversationId, onSelectConversation, onDeleteConversation, onRenameConversation, onSelect }: ConvListProps) {
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const renameInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (renamingId) renameInputRef.current?.focus();
  }, [renamingId]);

  const startRename = (e: React.MouseEvent, id: string, title: string) => {
    e.stopPropagation();
    setRenamingId(id);
    setRenameValue(title);
  };

  const commitRename = (id: string) => {
    const trimmed = renameValue.trim();
    if (trimmed) onRenameConversation(id, trimmed);
    setRenamingId(null);
  };

  return (
    <div className="flex-1 space-y-2 overflow-y-auto p-3">
      {conversations.map((conv) => (
        <div
          key={conv.id}
          role="button"
          tabIndex={0}
          onClick={() => { if (renamingId !== conv.id) { onSelectConversation(conv.id); onSelect?.(); } }}
          onKeyDown={(e) => { if (renamingId !== conv.id && (e.key === 'Enter' || e.key === ' ')) { onSelectConversation(conv.id); onSelect?.(); } }}
          className={`w-full cursor-pointer rounded-2xl border p-3 text-left transition ${
            selectedConversationId === conv.id
              ? 'border-primary-300 bg-primary-50'
              : 'border-transparent bg-white hover:border-slate-200 hover:bg-slate-50'
          }`}
        >
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0 flex-1">
              <HiChat className="h-4 w-4 text-primary-700" />
              {renamingId === conv.id ? (
                <input
                  ref={renameInputRef}
                  type="text"
                  value={renameValue}
                  onChange={(e) => setRenameValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') commitRename(conv.id);
                    if (e.key === 'Escape') setRenamingId(null);
                    e.stopPropagation();
                  }}
                  onClick={(e) => e.stopPropagation()}
                  onBlur={() => commitRename(conv.id)}
                  className="mt-2 w-full rounded border border-primary-300 px-1.5 py-0.5 text-sm text-slate-900 focus:outline-none focus:ring-1 focus:ring-primary-500"
                />
              ) : (
                <h3 className="mt-2 line-clamp-2 text-sm font-semibold text-slate-900">{conv.title}</h3>
              )}
              <p className="mt-1 text-xs text-slate-500">{formatTime(conv.updatedAt)}</p>
            </div>
            <div className="flex shrink-0 items-center gap-0.5">
              <button
                type="button"
                onClick={(e) => startRename(e, conv.id, conv.title)}
                title="重新命名"
                className="rounded-lg p-1 text-slate-400 transition hover:bg-slate-100 hover:text-slate-600"
              >
                <HiPencil className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); onDeleteConversation(conv.id); }}
                title="刪除對話"
                className="rounded-lg p-1 text-rose-400 transition hover:bg-rose-50 hover:text-rose-600"
              >
                <HiTrash className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

export default function CourseSearchPage() {
  const { user, loading: authLoading, loginWithGoogle } = useAuth();
  const liveAi = useLiveAiAccess();
  const [conversations, setConversations]       = useState<Conversation[]>([]);
  const [selectedConversationId, setSelectedConversationId] = useState('');
  const [inputMessage, setInputMessage]         = useState('');
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(true);
  const [isMobileConversationOpen, setIsMobileConversationOpen] = useState(false);
  const [sessionsLoaded, setSessionsLoaded]     = useState(false);

  // 串流狀態
  const [isStreaming, setIsStreaming]   = useState(false);
  const [activeTools, setActiveTools]   = useState<ToolIndicator[]>([]);
  const [isVerifying, setIsVerifying]   = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const activeTraceRef = useRef<{ toolCalls: ToolTraceItem[]; poolSize: number }>({ toolCalls: [], poolSize: 0 });

  // 原地編輯狀態
  const [editingMsgIdx, setEditingMsgIdx]       = useState(-1);
  const [editingContent, setEditingContent]     = useState('');

  // 右側推薦課程
  const [panelCourses, setPanelCourses] = useState<CourseCard[]>([]);

  // Modal
  const [selectedCourse, setSelectedCourse] = useState<CourseCard | null>(null);

  const sessionIds    = useRef<Record<string, string>>({});
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef   = useRef<HTMLTextAreaElement>(null);

  const selectedConversation = useMemo(
    () => conversations.find((c) => c.id === selectedConversationId) ?? null,
    [conversations, selectedConversationId],
  );

  // textarea 自動增高，超過上限才顯示滾動條
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    const maxH = 120;
    const newH = Math.min(el.scrollHeight, maxH);
    el.style.height = `${newH}px`;
    el.style.overflowY = el.scrollHeight > maxH ? 'auto' : 'hidden';
  }, [inputMessage]);

  const createDefaultConversation = useCallback(() => {
    const nc: Conversation = {
      id: Date.now().toString(), title: '新對話',
      messages: [{ role: 'assistant', content: GREETING, timestamp: new Date() }],
      createdAt: new Date(), updatedAt: new Date(),
    };
    setConversations([nc]);
    setSelectedConversationId(nc.id);
  }, []);

  // ── 載入歷史對話（先拿 metadata，選到才 fetch 詳情）─────────────────────
  useEffect(() => {
    if (authLoading) return;
    let cancelled = false;

    void (async () => {
      await Promise.resolve();
      if (!user) {
        createDefaultConversation();
        setSessionsLoaded(true);
        return;
      }

      setSessionsLoaded(false);
      sessionIds.current = {};
      setPanelCourses([]);

      try {
        const sessions = await chatAPI.getSessions();
        if (cancelled) return;
        if (sessions.length === 0) {
          createDefaultConversation();
          setSessionsLoaded(true);
          return;
        }

        const stubs: Conversation[] = sessions.map(s => ({
          id:        s.session_id,
          title:     s.title || '未命名對話',
          messages:  [{ role: 'assistant' as const, content: GREETING, timestamp: new Date(s.updated_at) }],
          createdAt: new Date(s.updated_at),
          updatedAt: new Date(s.updated_at),
          isStub:    true,
        }));
        stubs.forEach(c => { sessionIds.current[c.id] = c.id; });
        setConversations(stubs);
        setSelectedConversationId(stubs[0].id);
        setSessionsLoaded(true);

        const detail = await chatAPI.getSession(stubs[0].id).catch(() => null);
        if (cancelled || !detail) return;
        const full = makeConvFromSession(detail);
        setConversations(prev => prev.map(c => c.id === full.id ? full : c));
        const lastMsg = [...full.messages].reverse().find(
          m => m.role === 'assistant' && (m.courseCards?.length ?? 0) > 0
        );
        if (lastMsg?.courseCards) setPanelCourses(lastMsg.courseCards);
      } catch {
        if (cancelled) return;
        createDefaultConversation();
        setSessionsLoaded(true);
      }
    })();

    return () => { cancelled = true; };
  }, [authLoading, user, createDefaultConversation]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [selectedConversation?.messages.length, isStreaming]);

  const updateConv = useCallback((id: string, updater: (c: Conversation) => Conversation) => {
    setConversations((prev) => prev.map((c) => (c.id === id ? updater(c) : c)));
  }, []);

  // ── 對話管理 ────────────────────────────────────────────────────────────
  const handleNewConversation = useCallback(() => {
    const nc: Conversation = {
      id: Date.now().toString(), title: '新對話',
      messages: [{ role: 'assistant', content: GREETING, timestamp: new Date() }],
      createdAt: new Date(), updatedAt: new Date(),
    };
    setConversations((prev) => [nc, ...prev]);
    setSelectedConversationId(nc.id);
    setInputMessage('');
    setIsMobileConversationOpen(false);
    setPanelCourses([]);
  }, []);

  const handleRenameConversation = useCallback((id: string, title: string) => {
    updateConv(id, (c) => ({ ...c, title }));
  }, [updateConv]);

  const handleDeleteConversation = (id: string) => {
    if (!window.confirm('確認要刪除這段對話嗎？此操作無法復原。')) return;
    const sid = sessionIds.current[id];
    if (sid) { chatAPI.clearSession(sid).catch(() => {}); delete sessionIds.current[id]; }
    setConversations((prev) => {
      const next = prev.filter((c) => c.id !== id);
      if (next.length === 0) {
        // 刪除最後一則：清空面板，建立新對話
        setPanelCourses([]);
        const nc: Conversation = {
          id: Date.now().toString(), title: '新對話',
          messages: [{ role: 'assistant', content: GREETING, timestamp: new Date() }],
          createdAt: new Date(), updatedAt: new Date(),
        };
        setSelectedConversationId(nc.id);
        return [nc];
      }
      if (selectedConversationId === id) {
        const nextConv = next[0];
        setSelectedConversationId(nextConv.id);
        const lastMsg = [...nextConv.messages].reverse().find(
          m => m.role === 'assistant' && (m.courseCards?.length ?? 0) > 0
        );
        setPanelCourses(lastMsg?.courseCards ?? []);
      }
      return next;
    });
    setInputMessage('');
  };

  // 選擇對話：stub 時 lazy load 完整詳情
  const handleSelectConversation = useCallback(async (id: string) => {
    setSelectedConversationId(id);
    setIsMobileConversationOpen(false);

    const conv = conversations.find(c => c.id === id);
    if (!conv) return;

    if (conv.isStub) {
      const detail = await chatAPI.getSession(id).catch(() => null);
      if (!detail) return;
      const full = makeConvFromSession(detail);
      setConversations(prev => prev.map(c => c.id === id ? full : c));
      const lastMsg = [...full.messages].reverse().find(
        m => m.role === 'assistant' && (m.courseCards?.length ?? 0) > 0
      );
      setPanelCourses(lastMsg?.courseCards ?? []);
      return;
    }

    const last = [...conv.messages].reverse().find(
      m => m.role === 'assistant' && !m.isStreaming && (m.courseCards?.length ?? 0) > 0
    );
    setPanelCourses(last?.courseCards ?? []);
  }, [conversations]);

  const handleShowMsgCourses = useCallback((msg: Message) => {
    setPanelCourses(msg.courseCards ?? []);
  }, []);

  // Ctrl+K / ⌘K 開新對話
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        handleNewConversation();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [handleNewConversation]);

  // ── 送出訊息（串流） ─────────────────────────────────────────────────────
  // sendText: 覆蓋 inputMessage；fromIdx: 從此索引截斷再送（編輯用）
  const doSend = (sendText?: string, fromIdx?: number) => {
    const text = (sendText !== undefined ? sendText : inputMessage).trim();
    if (!user || !liveAi.allowed || !text || !selectedConversation || isStreaming) return;

    if (sendText === undefined) setInputMessage('');
    const convId = selectedConversation.id;
    setIsStreaming(true);
    setActiveTools([]);
    setIsVerifying(false);
    activeTraceRef.current = { toolCalls: [], poolSize: 0 };

    const userMsg: Message   = { role: 'user',      content: text, timestamp: new Date() };
    const streamMsg: Message = { role: 'assistant',  content: '',   timestamp: new Date(), isStreaming: true };

    // 計算 streamIdx：如果有 fromIdx，訊息先截斷再加入，所以 streamMsg 在 fromIdx+1
    const baseCount = fromIdx !== undefined ? fromIdx : selectedConversation.messages.length;
    const streamIdx = baseCount + 1;

    updateConv(convId, (c) => {
      const base = fromIdx !== undefined ? c.messages.slice(0, fromIdx) : [...c.messages];
      return {
        ...c,
        updatedAt: new Date(),
        title: base.length === 1 ? text.slice(0, 30) : c.title,
        messages: [...base, userMsg, streamMsg],
      };
    });

    abortRef.current = chatStreamAPI.stream(
      text,
      {
        onToken: (tok) => {
          updateConv(convId, (c) => {
            const msgs = [...c.messages];
            if (msgs[streamIdx]) msgs[streamIdx] = { ...msgs[streamIdx], content: msgs[streamIdx].content + tok };
            return { ...c, messages: msgs };
          });
        },
        onToolStart: (tool, args) => {
          setActiveTools((prev) => [...prev, { name: tool, done: false }]);
          activeTraceRef.current.toolCalls.push({ tool, args, coursesFound: [], scores: [], scoreType: null, count: undefined });
        },
        onToolDone: (tool, count, coursesFound, scores, scoreType) => {
          setActiveTools((prev) =>
            prev.map((t) => (t.name === tool && !t.done ? { ...t, done: true, count } : t))
          );
          const calls = activeTraceRef.current.toolCalls;
          const last = [...calls].reverse().find((c) => c.tool === tool && c.count === undefined);
          if (last) {
            last.count = count;
            last.coursesFound = coursesFound ?? [];
            last.scores    = scores    ?? [];
            last.scoreType = scoreType ?? null;
          }
        },
        onVerifyStart: (poolSize) => {
          activeTraceRef.current.poolSize = poolSize;
          setIsVerifying(true);
        },
        onVerifyDone: (selected, filteredOut, method) => {
          setIsVerifying(false);
          (activeTraceRef.current as typeof activeTraceRef.current & { verifyResult?: unknown }).verifyResult = { selected, filteredOut, method };
        },
        onDone: (ev: StreamEvent) => {
          if (ev.session_id) sessionIds.current[convId] = ev.session_id;
          const cards = ev.course_cards ?? [];
          const tools = ev.tools_used   ?? [];

          const traceRef = activeTraceRef.current as typeof activeTraceRef.current & { verifyResult?: { selected: string[]; filteredOut: string[]; method?: 'tag' | 'llm' } };
          const debugTrace: DebugTrace = {
            toolCalls: traceRef.toolCalls,
            verify: traceRef.verifyResult
              ? { poolSize: traceRef.poolSize, ...traceRef.verifyResult }
              : null,
          };

          setPanelCourses(cards);

          updateConv(convId, (c) => {
            const msgs = [...c.messages];
            if (msgs[streamIdx]) {
              msgs[streamIdx] = {
                ...msgs[streamIdx],
                ...(ev.final_answer ? { content: ev.final_answer } : {}),
                isStreaming: false,
                courseCards: cards,
                toolsUsed:   tools,
                debugTrace,
              };
            }
            return { ...c, messages: msgs };
          });
          setIsStreaming(false);
          setIsVerifying(false);
          setActiveTools([]);
        },
        onError: (msg) => {
          updateConv(convId, (c) => {
            const msgs = [...c.messages];
            if (msgs[streamIdx]) {
              msgs[streamIdx] = {
                ...msgs[streamIdx],
                isStreaming: false,
                content: msgs[streamIdx].content || `抱歉，發生錯誤：${msg}`,
              };
            }
            return { ...c, messages: msgs };
          });
          setIsStreaming(false);
          setIsVerifying(false);
          setActiveTools([]);
        },
        onRestricted: (msg) => {
          // 後端判定目前帳號不能使用即時 AI：顯示說明而不是錯誤，並把輸入區換成說明元件
          liveAi.markRestricted(msg);
          updateConv(convId, (c) => {
            const msgs = [...c.messages];
            if (msgs[streamIdx]) {
              msgs[streamIdx] = { ...msgs[streamIdx], isStreaming: false, content: msgs[streamIdx].content || msg };
            }
            return { ...c, messages: msgs };
          });
          setIsStreaming(false);
          setIsVerifying(false);
          setActiveTools([]);
        },
      },
      sessionIds.current[convId] || undefined,
    );
  };

  const handleSendMessage = (e: React.FormEvent) => {
    e.preventDefault();
    doSend();
  };

  const handleAbort = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    if (!selectedConversation) return;
    const convId = selectedConversation.id;
    updateConv(convId, (c) => {
      const msgs = [...c.messages];
      const last = msgs.at(-1);
      if (last?.isStreaming) {
        msgs[msgs.length - 1] = {
          ...last,
          isStreaming: false,
          content: (last.content || '') + '…（已中止）',
        };
      }
      return { ...c, messages: msgs };
    });
    setIsStreaming(false);
    setIsVerifying(false);
    setActiveTools([]);
  };

  const handleSendEdit = () => {
    const text = editingContent.trim();
    if (!text || isStreaming) return;
    const fromIdx = editingMsgIdx;
    setEditingMsgIdx(-1);
    setEditingContent('');
    doSend(text, fromIdx);
  };

  const handleCancelEdit = () => {
    setEditingMsgIdx(-1);
    setEditingContent('');
  };


  if (!sessionsLoaded) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="flex items-center gap-3 text-gray-400">
          <span className="inline-block h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-primary-500" />
          載入對話記錄中…
        </div>
      </div>
    );
  }

  return (
    <div className="page-container-wide flex h-full flex-col py-3">
      {/* 頁頭 */}
      <div className="mb-3 flex flex-shrink-0 items-center justify-between">
        <div className="flex items-center gap-2">
          <h1 className="text-base font-bold text-primary-900">課程助理</h1>
          <span className="text-sm text-gray-400">/</span>
          <span className="text-sm text-gray-500">用 AI 對話方式快速整理課程方向與學習線索</span>
        </div>
        <button onClick={handleNewConversation} className="btn-primary flex items-center gap-2 px-3 py-1.5 text-sm">
          <HiPlus className="h-4 w-4" />
          新對話
        </button>
      </div>

      <div className="flex min-h-0 flex-1 gap-4">
        {/* Mobile 對話列表 */}
        {isMobileConversationOpen && (
          <div className="fixed inset-y-0 left-0 z-40 w-[min(20rem,calc(100vw-3rem))] lg:hidden">
            <div className="flex h-full">
              <div className="card flex min-w-0 flex-1 flex-col overflow-hidden rounded-l-none rounded-r-2xl border-l-0 shadow-xl">
                <div className="flex items-center justify-between gap-2 border-b border-gray-200 p-3">
                  <div className="flex items-center gap-2">
                    <HiMenu className="h-5 w-5 text-primary-700" />
                    <span className="text-sm font-semibold text-slate-800">對話列表</span>
                  </div>
                  <button type="button" onClick={() => setIsMobileConversationOpen(false)}
                    className="rounded-xl p-2 text-slate-600 transition hover:bg-slate-100">
                    <HiChevronLeft className="h-5 w-5" />
                  </button>
                </div>
                <div className="border-b border-gray-200 p-3">
                  <button type="button" onClick={handleNewConversation}
                    className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-primary-900 px-4 py-3 text-sm font-medium text-white transition hover:bg-primary-800">
                    <HiPlus className="h-4 w-4" />新對話
                  </button>
                </div>
                <ConvList
                  conversations={conversations}
                  selectedConversationId={selectedConversationId}
                  onSelectConversation={handleSelectConversation}
                  onDeleteConversation={handleDeleteConversation}
                  onRenameConversation={handleRenameConversation}
                  onSelect={() => setIsMobileConversationOpen(false)}
                />
              </div>
              <div className="flex w-11 items-center justify-center pl-2">
                <button type="button" onClick={() => setIsMobileConversationOpen(false)}
                  className="flex h-28 w-10 flex-col items-center justify-center gap-2 rounded-r-2xl bg-primary-900 px-2 text-white shadow-lg transition hover:bg-primary-800">
                  <HiChevronLeft className="h-5 w-5" />
                  <span className="[writing-mode:vertical-rl] text-xs tracking-[0.2em]">對話列表</span>
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Desktop 側欄（左） */}
        <aside className={`card hidden min-h-0 flex-shrink-0 overflow-hidden transition-all duration-300 lg:flex ${
          isSidebarCollapsed ? 'w-[64px]' : 'w-[260px]'
        }`}>
          <div className="flex w-full flex-col">
            <div className={`border-b border-gray-200 p-3 ${
              isSidebarCollapsed ? 'flex flex-col items-center gap-3' : 'flex items-center justify-between gap-2'
            }`}>
              {isSidebarCollapsed ? (
                <>
                  <button type="button" onClick={() => setIsSidebarCollapsed(false)}
                    className="rounded-xl p-2 text-slate-600 transition hover:bg-slate-100">
                    <HiChevronRight className="h-5 w-5" />
                  </button>
                  <button type="button" onClick={handleNewConversation}
                    className="rounded-xl bg-primary-900 p-2 text-white transition hover:bg-primary-800">
                    <HiPlus className="h-5 w-5" />
                  </button>
                </>
              ) : (
                <>
                  <span className="text-sm font-semibold text-slate-800">對話紀錄</span>
                  <button type="button" onClick={() => setIsSidebarCollapsed(true)}
                    className="rounded-xl p-2 text-slate-600 transition hover:bg-slate-100">
                    <HiChevronLeft className="h-5 w-5" />
                  </button>
                </>
              )}
            </div>
            {!isSidebarCollapsed ? (
              <ConvList
                conversations={conversations}
                selectedConversationId={selectedConversationId}
                onSelectConversation={handleSelectConversation}
                onDeleteConversation={handleDeleteConversation}
                onRenameConversation={handleRenameConversation}
              />
            ) : (
              <div className="flex-1 space-y-2 overflow-y-auto p-2">
                {conversations.map((conv) => (
                  <button key={conv.id} type="button"
                    onClick={() => handleSelectConversation(conv.id)}
                    className={`flex w-full justify-center rounded-2xl border p-2.5 transition ${
                      selectedConversationId === conv.id
                        ? 'border-primary-300 bg-primary-50'
                        : 'border-transparent bg-white hover:border-slate-200 hover:bg-slate-50'
                    }`}>
                    <HiChat className="h-5 w-5 text-primary-700" />
                  </button>
                ))}
              </div>
            )}
          </div>
        </aside>

        {/* 主聊天區（中） */}
        <div className="flex min-h-0 flex-1 flex-col">
          {selectedConversation ? (
            <div className="relative h-full">
              <button type="button" onClick={() => setIsMobileConversationOpen(true)}
                className="fixed left-2 top-1/2 z-10 flex h-28 w-10 -translate-y-1/2 flex-col items-center justify-center gap-2 rounded-r-2xl bg-primary-900 px-2 text-white shadow-lg transition hover:bg-primary-800 lg:hidden">
                <HiChevronRight className="h-5 w-5" />
                <span className="[writing-mode:vertical-rl] text-xs tracking-[0.2em]">對話列表</span>
              </button>

              <div className="card flex h-full flex-col">
                {/* 標題列 */}
                <div className="flex flex-shrink-0 items-center border-b border-gray-200 px-4 py-3">
                  <h2 className="truncate text-sm font-semibold text-gray-800">{selectedConversation.title}</h2>
                </div>

                {/* 訊息列表 */}
                <div className="flex-1 space-y-3 overflow-y-auto p-4">
                  {selectedConversation.messages.map((msg, idx, arr) => {
                    const lastUserIdx = arr.reduceRight(
                      (acc, m, i) => (acc === -1 && m.role === 'user' ? i : acc), -1,
                    );
                    const isEditing = editingMsgIdx === idx;
                    const canEdit   = msg.role === 'user' && idx === lastUserIdx && !isStreaming && liveAi.allowed;
                    return (
                    <div key={idx} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                      <div className={msg.role === 'user' ? 'max-w-[82%]' : 'w-full'}>
                        {/* 訊息氣泡 */}
                        <div
                          className={`rounded-2xl p-4 ${
                            msg.role === 'user'
                              ? 'bg-primary-700 text-white'
                              : 'bg-gray-100 text-gray-900'
                          }`}
                          title={msg.timestamp.toLocaleString('zh-TW')}
                        >
                          {msg.role === 'user' ? (
                            <p className="whitespace-pre-line text-sm leading-relaxed">{msg.content}</p>
                          ) : (
                            <HighlightedAnswer
                              text={msg.content}
                              courseCards={msg.courseCards ?? []}
                              onCourseClick={setSelectedCourse}
                            />
                          )}
                          {msg.isStreaming && (
                            <span className="ml-0.5 inline-block h-4 w-0.5 animate-pulse bg-gray-500" />
                          )}
                        </div>

                        {/* 編輯按鈕（氣泡下方，最後一則 user 訊息且未在編輯中） */}
                        {canEdit && !isEditing && (
                          <div className="mt-1 flex justify-end">
                            <button
                              type="button"
                              onClick={() => { setEditingMsgIdx(idx); setEditingContent(msg.content); }}
                              className="flex items-center gap-1 rounded-lg px-2 py-0.5 text-xs text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600"
                            >
                              <HiPencil className="h-3 w-3" />
                              編輯
                            </button>
                          </div>
                        )}

                        {/* 原地編輯區 */}
                        {canEdit && isEditing && (
                          <div className="mt-2">
                            <textarea
                              value={editingContent}
                              onChange={(e) => setEditingContent(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSendEdit(); }
                                if (e.key === 'Escape') handleCancelEdit();
                              }}
                              rows={2}
                              autoFocus
                              className="w-full resize-none rounded-xl border border-primary-300 px-3 py-2 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-primary-300"
                            />
                            <div className="mt-1.5 flex justify-end gap-2">
                              <button
                                type="button"
                                onClick={handleCancelEdit}
                                className="rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-xs text-gray-500 transition hover:bg-gray-50"
                              >
                                返回
                              </button>
                              <button
                                type="button"
                                onClick={handleSendEdit}
                                disabled={!editingContent.trim()}
                                className="flex items-center gap-1.5 rounded-lg bg-primary-700 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-primary-800 disabled:opacity-40"
                              >
                                <HiPaperAirplane className="h-3.5 w-3.5" />
                                傳送
                              </button>
                            </div>
                          </div>
                        )}

                        {/* Assistant 訊息的附加資訊 */}
                        {msg.role === 'assistant' && !msg.isStreaming && (
                          <div className="mt-2 space-y-1.5">
                            {(msg.courseCards?.length ?? 0) > 0 && (
                              <div className="flex flex-wrap items-center gap-2">
                                <button
                                  type="button"
                                  onClick={() => handleShowMsgCourses(msg)}
                                  className="flex items-center gap-1.5 rounded-xl border border-primary-200 bg-white px-3 py-1.5 text-xs font-medium text-primary-600 shadow-sm transition hover:bg-primary-50"
                                >
                                  <HiBookOpen className="h-3.5 w-3.5" />
                                  查看此次推薦課程（{msg.courseCards!.length} 門）
                                </button>
                                {(msg.toolsUsed?.length ?? 0) > 0 && (
                                  <span className="text-xs text-gray-400">
                                    {msg.toolsUsed!.map((t) => TOOL_LABELS[t] ?? t).join(' · ')}
                                  </span>
                                )}
                              </div>
                            )}
                            {msg.debugTrace && <DebugTracePanel trace={msg.debugTrace} />}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                  })}

                  {/* 工具呼叫進度 */}
                  {isStreaming && activeTools.length > 0 && (
                    <div className="flex justify-start">
                      <div className="rounded-2xl bg-gray-50 px-4 py-3 text-xs text-gray-500 shadow-sm">
                        <div className="space-y-1">
                          {activeTools.map((t, i) => (
                            <div key={i} className="flex items-center gap-2">
                              {t.done ? (
                                <span className="text-green-500">✓</span>
                              ) : (
                                <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-gray-300 border-t-primary-500" />
                              )}
                              <span className={t.done ? 'text-gray-400' : 'text-gray-700'}>
                                {TOOL_LABELS[t.name] ?? t.name}
                                {t.done && t.count != null && ` (${t.count} 筆)`}
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                    </div>
                  )}

                  {isVerifying && (
                    <div className="flex justify-start">
                      <div className="flex items-center gap-2 rounded-2xl bg-amber-50 px-4 py-2.5 text-xs text-amber-700 shadow-sm">
                        <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-amber-200 border-t-amber-500" />
                        正在驗證推薦課程…
                      </div>
                    </div>
                  )}

                  {isStreaming && activeTools.length === 0 &&
                    !selectedConversation.messages.at(-1)?.content && (
                    <div className="flex justify-start">
                      <div className="rounded-2xl bg-gray-100 p-4">
                        <div className="flex space-x-2">
                          <div className="h-2 w-2 animate-bounce rounded-full bg-gray-400" />
                          <div className="h-2 w-2 animate-bounce rounded-full bg-gray-400 [animation-delay:100ms]" />
                          <div className="h-2 w-2 animate-bounce rounded-full bg-gray-400 [animation-delay:200ms]" />
                        </div>
                      </div>
                    </div>
                  )}

                  <div ref={messagesEndRef} />
                </div>

                {/* 輸入框 / 登入提示 / 即時 AI 權限說明 */}
                {!user ? (
                  <div className="border-t border-gray-200 p-4">
                    <div className="flex flex-col items-center gap-3 rounded-2xl bg-slate-50 px-4 py-5 text-center">
                      <div className="flex h-10 w-10 items-center justify-center rounded-full bg-slate-100">
                        <HiLockClosed className="h-5 w-5 text-slate-400" />
                      </div>
                      <div>
                        <p className="text-sm font-semibold text-slate-700">需要登入才能使用 AI 對話</p>
                        <p className="mt-0.5 text-xs text-slate-400">登入後即可開始提問，並自動儲存對話記錄</p>
                      </div>
                      <button
                        type="button"
                        onClick={loginWithGoogle}
                        className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-700 shadow-sm transition hover:bg-slate-50 hover:shadow"
                      >
                        <svg aria-hidden="true" viewBox="0 0 24 24" className="h-4 w-4 shrink-0">
                          <path fill="#4285F4" d="M21.6 12.2c0-.7-.1-1.3-.2-1.9H12v3.6h5.4c-.2 1.2-.9 2.3-2 3v2.4h3.2c1.9-1.7 3-4.2 3-7.1z" />
                          <path fill="#34A853" d="M12 22c2.7 0 5-.9 6.6-2.5l-3.2-2.4c-.9.6-2 1-3.4 1-2.6 0-4.8-1.8-5.6-4.1H3.1v2.5C4.8 19.8 8.1 22 12 22z" />
                          <path fill="#FBBC05" d="M6.4 14c-.2-.6-.3-1.3-.3-2s.1-1.4.3-2V7.5H3.1C2.4 8.9 2 10.4 2 12s.4 3.1 1.1 4.5L6.4 14z" />
                          <path fill="#EA4335" d="M12 5.9c1.5 0 2.8.5 3.8 1.5l2.8-2.8C16.9 3 14.7 2 12 2 8.1 2 4.8 4.2 3.1 7.5L6.4 10c.8-2.3 3-4.1 5.6-4.1z" />
                        </svg>
                        使用 Google 帳號登入
                      </button>
                    </div>
                  </div>
                ) : liveAi.loading ? (
                  <LiveAiPendingSlot compact />
                ) : !liveAi.allowed ? (
                  <LiveAiDisabledNotice
                    assistant="course"
                    message={liveAi.message}
                  />
                ) : (
                  <form onSubmit={handleSendMessage} className="border-t border-gray-200 p-3">
                    <div className="flex items-end gap-3">
                      <textarea
                        ref={textareaRef}
                        value={inputMessage}
                        onChange={(e) => setInputMessage(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' && !e.shiftKey) {
                            e.preventDefault();
                            doSend();
                          }
                        }}
                        disabled={isStreaming}
                        placeholder="輸入你想查詢的課程、學院或學習方向（Enter 送出，Shift+Enter 換行）"
                        rows={1}
                        className="flex-1 resize-none overflow-y-hidden rounded-xl border border-gray-300 px-4 py-3 text-sm leading-6 transition-colors focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-slate-400"
                      />
                      {isStreaming ? (
                        <button
                          type="button"
                          onClick={handleAbort}
                          className="btn-secondary flex-shrink-0 self-end"
                          aria-label="中止回應"
                        >
                          <HiStop className="h-5 w-5" />
                        </button>
                      ) : (
                        <button
                          type="submit"
                          disabled={!inputMessage.trim()}
                          className="btn-primary flex-shrink-0 self-end disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          <HiPaperAirplane className="h-5 w-5" />
                        </button>
                      )}
                    </div>
                  </form>
                )}
              </div>
            </div>
          ) : (
            <div className="card flex h-full flex-col items-center justify-center">
              <HiChat className="mb-4 h-16 w-16 text-gray-300" />
              <p className="mb-2 text-gray-600">請先選擇一個對話，或建立新的對話。</p>
            </div>
          )}
        </div>

        {/* 右側課程面板（Desktop） */}
        <aside className="card hidden min-h-0 w-[280px] flex-shrink-0 overflow-hidden lg:flex flex-col">
          <CourseSidePanel
            courses={panelCourses}
            onCourseClick={setSelectedCourse}
          />
        </aside>
      </div>

      {selectedCourse && (
        <CourseDetailModal
          course={selectedCourse}
          onClose={() => setSelectedCourse(null)}
        />
      )}
    </div>
  );
}
