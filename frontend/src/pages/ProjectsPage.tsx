import { useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  HiAcademicCap,
  HiArrowLeft,
  HiChat,
  HiChevronDown,
  HiChevronLeft,
  HiChevronRight,
  HiClock,
  HiLockClosed,
  HiPaperAirplane,
  HiPencil,
  HiPlus,
  HiSearch,
  HiStop,
  HiTrash,
  HiUser,
} from 'react-icons/hi';
import { projectAPI } from '../api/services';
import type { PdfConversationSummary } from '../api/services';
import {
  COLLEGE_ORDER,
  getDeptCollege,
  deptSortKey,
  sortDeptsByCollegeOrder,
} from '../constants/colleges';
import PdfViewer from '../components/PdfViewer';
import LiveAiDisabledNotice, { LiveAiPendingSlot } from '../components/LiveAiDisabledNotice';
import { useAuth } from '../auth/AuthContext';
import { useLiveAiAccess } from '../hooks/useLiveAiAccess';
import type { LiveAiStatus } from '../api/liveAi';
import type { Project } from '../types';

type ChatMessage = {
  role: 'user' | 'assistant';
  content: string;
  sources?: string[];
};

type MobileOutlineView = 'list' | 'detail';

export default function ProjectsPage() {
  const { user, loginWithGoogle } = useAuth();
  const liveAi = useLiveAiAccess();
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProject] = useState<Project | null>(null);
  const [viewMode, setViewMode] = useState<'outline' | 'pdf-chat'>('outline');
  const [mobileOutlineView, setMobileOutlineView] = useState<MobileOutlineView>('list');
  const [isMobileChatOpen, setIsMobileChatOpen] = useState(false);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [inputMessage, setInputMessage] = useState('');
  const [chatLoading, setChatLoading] = useState(false);
  const [activeAgent, setActiveAgent] = useState<{ name: string; label: string } | null>(null);
  const [currentStage, setCurrentStage] = useState<string>('');
  const [threadId, setThreadId] = useState<string | undefined>(undefined);
  const [streamingText, setStreamingText] = useState('');
  const abortCtrlRef = useRef<AbortController | null>(null);
  const streamingTextRef = useRef('');
  const [loading, setLoading] = useState(true);
  const [pdfConversations, setPdfConversations] = useState<PdfConversationSummary[]>([]);
  const [filterYear, setFilterYear] = useState('');
  const [filterDept, setFilterDept] = useState('');
  const [filterTitle, setFilterTitle] = useState('');
  const [recentChats, setRecentChats] = useState<{
    document_id: number;
    thread_id: string;
    title: string;
    created_at: string | null;
  }[]>([]);
  const [leftPanelTab, setLeftPanelTab] = useState<'projects' | 'recent'>('projects');

  useEffect(() => {
    projectAPI
      .getProjects()
      .then((data) => {
        setProjects(data);
      })
      .catch((error) => {
        console.error('Failed to load projects:', error);
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!user) { setRecentChats([]); return; }
    projectAPI.listRecentChats()
      .then(setRecentChats)
      .catch(() => {});
  }, [user]);

  const years = useMemo(
    () => [...new Set(projects.map((project) => project.year))].sort((a, b) => b.localeCompare(a)),
    [projects]
  );

  const departments = useMemo(
    () => sortDeptsByCollegeOrder([...new Set(projects.map((p) => p.department))]),
    [projects]
  );

  const filteredProjects = useMemo(() => {
    const q = filterTitle.trim().toLowerCase();
    const filtered = projects.filter(
      (p) =>
        (!filterYear || p.year === filterYear) &&
        (!filterDept || p.department === filterDept) &&
        (!q || p.title.toLowerCase().includes(q))
    );
    return filtered.sort((a, b) => {
      const ca = getDeptCollege(a.department), cb = getDeptCollege(b.department);
      const ia = COLLEGE_ORDER.indexOf(ca as (typeof COLLEGE_ORDER)[number]);
      const ib = COLLEGE_ORDER.indexOf(cb as (typeof COLLEGE_ORDER)[number]);
      const ra = ia === -1 ? COLLEGE_ORDER.length : ia;
      const rb = ib === -1 ? COLLEGE_ORDER.length : ib;
      if (ra !== rb) return ra - rb;
      const ka = deptSortKey(a.department), kb = deptSortKey(b.department);
      if (ka !== kb) return ka - kb;
      const dc = a.department.localeCompare(b.department, 'zh-Hant');
      if (dc !== 0) return dc;
      return b.year.localeCompare(a.year);
    });
  }, [projects, filterYear, filterDept, filterTitle]);

  const matchedRecentChats = useMemo(
    () => recentChats.flatMap(rc => {
      const proj = projects.find(p => p.documentId === rc.document_id);
      return proj ? [{ ...rc, project: proj }] : [];
    }),
    [recentChats, projects]
  );

  const clearSelectedIfFiltered = (nextYear: string, nextDept: string) => {
    if (!selectedProject) return;
    const stillExists = projects.some(
      (p) =>
        p.id === selectedProject.id &&
        (!nextYear || p.year === nextYear) &&
        (!nextDept || p.department === nextDept),
    );
    if (stillExists) return;
    setSelectedProject(null);
    setChatMessages([]);
    setInputMessage('');
    setViewMode('outline');
    setMobileOutlineView('list');
    setIsMobileChatOpen(false);
  };

  const handleFilterYearChange = (year: string) => {
    setFilterYear(year);
    clearSelectedIfFiltered(year, filterDept);
  };

  const handleFilterDeptChange = (dept: string) => {
    setFilterDept(dept);
    clearSelectedIfFiltered(filterYear, dept);
  };

  const handleSelectProject = (project: Project) => {
    setSelectedProject(project);
    setChatMessages([createWelcomeMessage(project)]);
    setInputMessage('');
    setViewMode('outline');
    setMobileOutlineView('detail');
    setIsMobileChatOpen(false);
    setThreadId(undefined);
    streamingTextRef.current = '';
    setStreamingText('');
    setChatLoading(false);
    setPdfConversations([]);
    abortCtrlRef.current?.abort();
    abortCtrlRef.current = null;
    if (user) {
      projectAPI.listConversations(project.id)
        .then((convs) => setPdfConversations(convs))
        .catch(() => {});
    }
  };

  const handleBackToOutline = () => {
    setViewMode('outline');
    setMobileOutlineView('detail');
    setIsMobileChatOpen(false);
  };

  const handleBackToProjectList = () => {
    setMobileOutlineView('list');
  };

  const handleOpenPdfChat = () => {
    if (!selectedProject) return;
    setViewMode('pdf-chat');
    setIsMobileChatOpen(false);
    if (chatMessages.length === 0) {
      setChatMessages([createWelcomeMessage(selectedProject)]);
    }
  };

  const doSendProjectMessage = (msg: string, fromIdx?: number) => {
    if (!user || !liveAi.allowed || !selectedProject || chatLoading) return;
    setChatLoading(true);
    setStreamingText('');
    setChatMessages((prev) => {
      const base = fromIdx !== undefined ? prev.slice(0, fromIdx) : [...prev];
      return [...base, { role: 'user', content: msg }];
    });

    const ctrl = projectAPI.streamChat(
      selectedProject.id,
      msg,
      {
        onToken: (token) => {
          streamingTextRef.current += token;
          setStreamingText(streamingTextRef.current);
        },
        onReplace: (text) => {
          streamingTextRef.current = text;
          setStreamingText(text);
        },
        onSessionId: (id) => {
          setThreadId(id || undefined);
        },
        onAgentStart: (agent, label) => {
          setActiveAgent({ name: agent, label });
          setCurrentStage('');
        },
        onStage: (text) => {
          setCurrentStage(text);
        },
        onDone: (sessionId, _cancelled, sources) => {
          setActiveAgent(null);
          setCurrentStage('');
          const finalText = streamingTextRef.current;
          streamingTextRef.current = '';
          setStreamingText('');
          if (finalText) {
            setChatMessages((msgs) => [
              ...msgs,
              { role: 'assistant', content: finalText, sources: sources?.length ? sources : undefined },
            ]);
          }
          setChatLoading(false);
          const newThreadId = sessionId || undefined;
          setThreadId(newThreadId);
          abortCtrlRef.current = null;
          if (selectedProject && newThreadId) {
            projectAPI.listConversations(selectedProject.id)
              .then((convs) => setPdfConversations(convs))
              .catch(() => {});
          }
        },
        onError: (errMsg) => {
          setActiveAgent(null);
          setCurrentStage('');
          console.error('Project stream error:', errMsg);
          streamingTextRef.current = '';
          setStreamingText('');
          setChatMessages((prev) => [
            ...prev,
            { role: 'assistant', content: '目前暫時無法回應，請稍後再試一次。' },
          ]);
          setChatLoading(false);
          abortCtrlRef.current = null;
        },
        onRestricted: (restrictedMsg) => {
          // 後端判定目前帳號不能使用即時 AI：顯示說明而不是錯誤，並把輸入區換成說明元件
          liveAi.markRestricted(restrictedMsg);
          setActiveAgent(null);
          setCurrentStage('');
          streamingTextRef.current = '';
          setStreamingText('');
          setChatMessages((prev) => [...prev, { role: 'assistant', content: restrictedMsg }]);
          setChatLoading(false);
          abortCtrlRef.current = null;
        },
      },
      threadId,
    );
    abortCtrlRef.current = ctrl;
  };

  const handleSendMessage = (event: FormEvent) => {
    event.preventDefault();
    if (!inputMessage.trim()) return;
    const text = inputMessage.trim();
    setInputMessage('');
    doSendProjectMessage(text);
  };

  const handleSendEditedMessage = (newContent: string, fromIdx: number) => {
    doSendProjectMessage(newContent, fromIdx);
  };

  const handleCancelStream = () => {
    if (!abortCtrlRef.current || !selectedProject || !threadId) return;
    abortCtrlRef.current.abort();
    abortCtrlRef.current = null;
    if (threadId) projectAPI.cancelChat(selectedProject.id, threadId).catch(() => {});
    const finalText = streamingTextRef.current;
    streamingTextRef.current = '';
    setStreamingText('');
    if (finalText) {
      setChatMessages((msgs) => [...msgs, { role: 'assistant', content: finalText + '…（已中止）' }]);
    }
    setChatLoading(false);
    setActiveAgent(null);
    setCurrentStage('');
  };

  const handleNewPdfConversation = () => {
    if (!selectedProject) return;
    abortCtrlRef.current?.abort();
    abortCtrlRef.current = null;
    streamingTextRef.current = '';
    setStreamingText('');
    setChatLoading(false);
    setActiveAgent(null);
    setCurrentStage('');
    setThreadId(undefined);
    setChatMessages([createWelcomeMessage(selectedProject)]);
  };

  const handleSwitchPdfConversation = async (targetThreadId: string) => {
    if (!selectedProject || chatLoading || targetThreadId === threadId) return;
    abortCtrlRef.current?.abort();
    abortCtrlRef.current = null;
    streamingTextRef.current = '';
    setStreamingText('');
    setChatLoading(true);
    setThreadId(targetThreadId);
    try {
      const msgs = await projectAPI.loadConversationMessages(selectedProject.id, targetThreadId);
      setChatMessages([createWelcomeMessage(selectedProject), ...msgs]);
    } catch {
      setChatMessages([createWelcomeMessage(selectedProject)]);
    } finally {
      setChatLoading(false);
      setActiveAgent(null);
      setCurrentStage('');
    }
  };

  const handleDeletePdfConversation = async (targetThreadId: string) => {
    if (!selectedProject) return;
    if (!window.confirm('確定要刪除此對話嗎？')) return;
    try {
      await projectAPI.deleteConversation(selectedProject.id, targetThreadId);
    } catch {
      return;
    }
    setPdfConversations((prev) => prev.filter((c) => c.thread_id !== targetThreadId));
    if (targetThreadId === threadId) {
      streamingTextRef.current = '';
      setStreamingText('');
      setChatLoading(false);
      setActiveAgent(null);
      setCurrentStage('');
      abortCtrlRef.current?.abort();
      abortCtrlRef.current = null;
      setThreadId(undefined);
      setChatMessages([createWelcomeMessage(selectedProject)]);
    }
  };

  const handleSelectFromRecentChat = (project: Project, targetThreadId: string) => {
    abortCtrlRef.current?.abort();
    abortCtrlRef.current = null;
    streamingTextRef.current = '';
    setStreamingText('');
    setChatLoading(false);
    setActiveAgent(null);
    setCurrentStage('');
    setSelectedProject(project);
    setChatMessages([createWelcomeMessage(project)]);
    setInputMessage('');
    setViewMode('outline');
    setMobileOutlineView('detail');
    setIsMobileChatOpen(false);
    setThreadId(targetThreadId);
    setPdfConversations([]);
    if (user) {
      projectAPI.listConversations(project.id)
        .then((convs) => setPdfConversations(convs))
        .catch(() => {});
    }
  };

  const handleContinueRecentChat = async () => {
    if (!selectedProject || !threadId) return;
    setViewMode('pdf-chat');
    setIsMobileChatOpen(false);
    setChatLoading(true);
    try {
      const msgs = await projectAPI.loadConversationMessages(selectedProject.id, threadId);
      setChatMessages([createWelcomeMessage(selectedProject), ...msgs]);
    } catch {
      setChatMessages([createWelcomeMessage(selectedProject)]);
    } finally {
      setChatLoading(false);
    }
  };

  const getCollegeLabel = getDeptCollege;

  const getCollegeBadgeClass = (college: string) => {
    switch (college) {
      case '文學院':
        return 'bg-blue-100 text-blue-800';
      case '理學院':
        return 'bg-cyan-100 text-cyan-800';
      case '工學院':
        return 'bg-orange-100 text-orange-800';
      case '管理學院':
        return 'bg-green-100 text-green-800';
      case '資訊電機學院':
        return 'bg-indigo-100 text-indigo-800';
      case '地球科學學院':
        return 'bg-teal-100 text-teal-800';
      case '客家學院':
        return 'bg-rose-100 text-rose-800';
      case '生醫理工學院':
        return 'bg-pink-100 text-pink-800';
      default:
        return 'bg-gray-100 text-gray-800';
    }
  };

  const getPdfUrl = (project: Project) => {
    const apiUrl = import.meta.env.VITE_API_URL || 'http://localhost:8000';
    const baseUrl = apiUrl.replace('/api', '');

    if (project.pdfUrl) {
      if (project.pdfUrl.startsWith('http://') || project.pdfUrl.startsWith('https://')) {
        return project.pdfUrl;
      }
      if (project.pdfUrl.startsWith('/')) {
        return `${baseUrl}${project.pdfUrl}`;
      }
      return project.pdfUrl;
    }

    if (!project.pdfPath) return '';
    const encodedPath = project.pdfPath.split('\\').map(encodeURIComponent).join('/');
    return `${baseUrl}/pdfs/${encodedPath}`;
  };

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center">
          <div className="mx-auto mb-4 h-12 w-12 animate-spin rounded-full border-b-2 border-primary-600"></div>
          <p className="text-gray-600">正在載入研究計畫...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="page-container-wide flex h-full flex-col py-3">
      {viewMode !== 'pdf-chat' ? (
        <div className="mb-3 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex min-w-0 items-center gap-2">
            <h1 className="text-base font-bold text-primary-900">專題成果</h1>
            <span className="text-sm text-gray-400">/</span>
            <span className="text-sm text-gray-500">瀏覽歷年專題，並可進一步開啟 PDF 與 AI 問答。</span>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative">
              <HiSearch className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-gray-400" />
              <input
                type="text"
                value={filterTitle}
                onChange={(e) => setFilterTitle(e.target.value)}
                placeholder="搜尋標題…"
                className="rounded-md border border-gray-300 py-1.5 pl-8 pr-3 text-sm focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500"
              />
            </div>
            <select
              value={filterYear}
              onChange={(event) => handleFilterYearChange(event.target.value)}
              className="rounded-md border border-gray-300 px-2 py-1.5 text-sm focus:border-primary-500 focus:ring-2 focus:ring-primary-500"
            >
              <option value="">全部年份</option>
              {years.map((year) => (
                <option key={year} value={year}>
                  {year}
                </option>
              ))}
            </select>
            <DeptSelect
              value={filterDept}
              onChange={handleFilterDeptChange}
              departments={departments}
              getCollegeLabel={getCollegeLabel}
            />
          </div>
        </div>
      ) : null}

      {viewMode === 'outline' ? (
        <>
          <div className="flex min-h-0 flex-1 flex-col lg:hidden">
            {mobileOutlineView === 'detail' && selectedProject ? (
              <div className="card flex-1 overflow-y-auto p-6">
                <div className="mb-4">
                  <button
                    type="button"
                    onClick={handleBackToProjectList}
                    className="flex items-center gap-1 text-sm text-primary-600 transition-colors hover:text-primary-800"
                  >
                    <HiArrowLeft className="h-4 w-4" />
                    返回專題列表
                  </button>
                </div>

                <ProjectSummary
                  project={selectedProject}
                  getCollegeLabel={getCollegeLabel}
                  getCollegeBadgeClass={getCollegeBadgeClass}
                />

                <div className="mt-8 flex justify-end">
                  <button type="button" onClick={handleOpenPdfChat} className="btn-primary flex items-center gap-2">
                    <HiChat className="h-5 w-5" />
                    查看 PDF 與 AI 對話
                  </button>
                </div>
              </div>
            ) : (
              <div className="flex min-h-0 flex-1 flex-col">
                <div className="flex-1 overflow-y-auto overflow-x-hidden [scrollbar-gutter:stable_both-edges]">
                  <div className="space-y-3 px-1 pt-2 pb-2 pr-4">
                    {filteredProjects.map((project) => (
                      <div
                        key={project.id}
                        className={`card cursor-pointer p-4 transition duration-200 ease-out hover:border-primary-200 hover:shadow-medium active:scale-[0.99] ${
                          selectedProject?.id === project.id ? 'border-primary-500 shadow-medium' : ''
                        }`}
                        onClick={() => handleSelectProject(project)}
                      >
                        <ProjectCardContent
                          project={project}
                          getCollegeLabel={getCollegeLabel}
                          getCollegeBadgeClass={getCollegeBadgeClass}
                          highlight={filterTitle}
                        />
                      </div>
                    ))}
                    {filteredProjects.length === 0 ? (
                      <div className="card p-6 text-center text-sm text-gray-500">目前沒有符合條件的研究計畫。</div>
                    ) : null}
                  </div>
                </div>
              </div>
            )}
          </div>

          <div className="hidden min-h-0 flex-1 lg:grid lg:grid-cols-3 lg:gap-4">
            <div className="flex min-h-0 flex-col">
              {/* 計數 + Tab 切換列 */}
              <div className="mb-2 flex shrink-0 items-center justify-between">
                <span className="text-xs text-gray-500">
                  共 <span className="font-semibold text-primary-700">{filteredProjects.length}</span> 筆
                </span>
                {user && matchedRecentChats.length > 0 && (
                  <div className="flex items-center gap-0.5 rounded-full bg-black/5 px-0.5 py-0.5">
                    <button
                      type="button"
                      onClick={() => setLeftPanelTab('projects')}
                      className={`flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium transition-all duration-150 focus:outline-none ${
                        leftPanelTab === 'projects'
                          ? 'bg-white text-primary-900 shadow-soft'
                          : 'text-slate-400 hover:text-slate-600'
                      }`}
                    >
                      <HiAcademicCap className="h-3.5 w-3.5" />
                      專題列表
                    </button>
                    <button
                      type="button"
                      onClick={() => setLeftPanelTab('recent')}
                      className={`flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium transition-all duration-150 focus:outline-none ${
                        leftPanelTab === 'recent'
                          ? 'bg-white text-primary-900 shadow-soft'
                          : 'text-slate-400 hover:text-slate-600'
                      }`}
                    >
                      <HiClock className="h-3.5 w-3.5" />
                      最近對話
                    </button>
                  </div>
                )}
              </div>

              {/* 專題列表 */}
              {leftPanelTab === 'projects' && (
                <div className="flex-1 overflow-y-auto overflow-x-hidden [scrollbar-gutter:stable_both-edges]">
                  <div className="space-y-3 px-1 pt-2 pb-2 pr-4">
                    {filteredProjects.map((project) => (
                      <div
                        key={project.id}
                        className={`card cursor-pointer p-4 transition duration-200 ease-out hover:border-primary-200 hover:shadow-medium active:scale-[0.99] ${
                          selectedProject?.id === project.id ? 'border-primary-500 shadow-medium' : ''
                        }`}
                        onClick={() => handleSelectProject(project)}
                      >
                        <ProjectCardContent
                          project={project}
                          getCollegeLabel={getCollegeLabel}
                          getCollegeBadgeClass={getCollegeBadgeClass}
                          highlight={filterTitle}
                        />
                      </div>
                    ))}
                    {filteredProjects.length === 0 ? (
                      <div className="card p-6 text-center text-sm text-gray-500">目前沒有符合條件的研究計畫。</div>
                    ) : null}
                  </div>
                </div>
              )}

              {/* 最近對話列表 */}
              {leftPanelTab === 'recent' && (
                <div className="flex-1 overflow-y-auto overflow-x-hidden [scrollbar-gutter:stable_both-edges]">
                  <div className="space-y-1 px-1 pt-2 pb-2 pr-4">
                    {matchedRecentChats.map(rc => (
                      <button
                        key={rc.thread_id}
                        type="button"
                        onClick={() => handleSelectFromRecentChat(rc.project, rc.thread_id)}
                        className={`flex w-full items-start gap-3 rounded-xl border px-3 py-3 text-left transition hover:border-primary-100 hover:bg-primary-50 ${
                          selectedProject?.id === rc.project.id && threadId === rc.thread_id
                            ? 'border-primary-200 bg-primary-50'
                            : 'border-transparent'
                        }`}
                      >
                        <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary-100">
                          <HiChat className="h-4 w-4 text-primary-500" />
                        </div>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-sm font-medium text-gray-800">{rc.project.title}</p>
                          <p className="truncate text-xs text-gray-500">{rc.title}</p>
                          <div className="mt-1.5 flex items-center gap-2">
                            <span className={`inline-block shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${getCollegeBadgeClass(getCollegeLabel(rc.project.department))}`}>
                              {rc.project.department}
                            </span>
                            <span className="shrink-0 rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-500">
                              {rc.project.year}
                            </span>
                            {rc.created_at && (
                              <span className="ml-auto shrink-0 text-[10px] text-gray-400">
                                {new Date(rc.created_at).toLocaleDateString('zh-TW')}
                              </span>
                            )}
                          </div>
                        </div>
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>

            <div className="min-h-0 overflow-y-auto lg:col-span-2">
              {selectedProject ? (
                <div className="card relative p-8">
                  <ProjectSummary
                    project={selectedProject}
                    getCollegeLabel={getCollegeLabel}
                    getCollegeBadgeClass={getCollegeBadgeClass}
                  />

                  <div className="flex justify-end gap-3">
                    {threadId && (
                      <button type="button" onClick={handleContinueRecentChat} className="btn-primary flex items-center gap-2">
                        <HiChat className="h-5 w-5" />
                        繼續對話
                      </button>
                    )}
                    <button type="button" onClick={() => { setThreadId(undefined); handleOpenPdfChat(); }} className={threadId ? 'btn-secondary flex items-center gap-2' : 'btn-primary flex items-center gap-2'}>
                      <HiChat className="h-5 w-5" />
                      {threadId ? '新對話' : '查看 PDF 與 AI 對話'}
                    </button>
                  </div>
                </div>
              ) : (
                <div className="card flex h-full min-h-[32rem] flex-col items-center justify-center">
                  <HiChat className="mb-4 h-16 w-16 text-gray-300" />
                  <p className="mb-2 text-gray-600">選擇一筆研究計畫即可查看詳細介紹。</p>
                  <p className="text-sm text-gray-500">接著可進一步開啟 PDF 與 AI 問答模式。</p>
                </div>
              )}
            </div>
          </div>
        </>
      ) : (
        <>
          <div className="relative min-h-0 flex-1 lg:hidden">
            <div className="card flex h-full flex-col overflow-hidden">
              <PdfPanel
                project={selectedProject}
                pdfUrl={selectedProject ? getPdfUrl(selectedProject) : ''}
                onBack={handleBackToOutline}
              />
            </div>

            <div
              className={`absolute inset-y-0 left-0 z-20 w-[min(22rem,calc(100vw-3rem))] transition-transform duration-300 ${
                isMobileChatOpen ? 'translate-x-0' : '-translate-x-[calc(100%-2.75rem)]'
              }`}
            >
              <div className="flex h-full">
                <ChatPanel
                  compact
                  messages={chatMessages}
                  streamingText={streamingText}
                  inputMessage={inputMessage}
                  chatLoading={chatLoading}
                  activeAgent={activeAgent}
                  currentStage={currentStage}
                  onInputChange={setInputMessage}
                  onSubmit={handleSendMessage}
                  onCancel={handleCancelStream}
                  onClose={() => setIsMobileChatOpen(false)}
                  isLoggedIn={!!user}
                  onLogin={loginWithGoogle}
                  liveAiStatus={liveAi.status}
                  liveAiMessage={liveAi.message}
                  conversations={pdfConversations}
                  activeThreadId={threadId}
                  onNewConversation={handleNewPdfConversation}
                  onSwitchConversation={handleSwitchPdfConversation}
                  onDeleteConversation={handleDeletePdfConversation}
                  onSendEdit={liveAi.allowed ? handleSendEditedMessage : undefined}
                />

                <div className="flex w-11 items-center justify-center pl-2">
                  <button
                    type="button"
                    onClick={() => setIsMobileChatOpen((prev) => !prev)}
                    className="flex h-28 w-10 flex-col items-center justify-center gap-2 rounded-r-2xl bg-primary-900 px-2 text-white shadow-lg transition hover:bg-primary-800"
                    aria-label={isMobileChatOpen ? '收起 AI 對話框' : '展開 AI 對話框'}
                  >
                    {isMobileChatOpen ? <HiChevronLeft className="h-5 w-5" /> : <HiChevronRight className="h-5 w-5" />}
                    <span className="[writing-mode:vertical-rl] text-xs tracking-[0.2em]">AI 對話</span>
                  </button>
                </div>
              </div>
            </div>
          </div>

          <div className="hidden min-h-0 flex-1 lg:grid lg:grid-cols-2 lg:gap-4">
            <div className="card flex h-full flex-col overflow-hidden">
              <PdfPanel
                project={selectedProject}
                pdfUrl={selectedProject ? getPdfUrl(selectedProject) : ''}
                onBack={handleBackToOutline}
              />
            </div>

            <ChatPanel
              messages={chatMessages}
              streamingText={streamingText}
              inputMessage={inputMessage}
              chatLoading={chatLoading}
              activeAgent={activeAgent}
              currentStage={currentStage}
              onInputChange={setInputMessage}
              onSubmit={handleSendMessage}
              onCancel={handleCancelStream}
              isLoggedIn={!!user}
              onLogin={loginWithGoogle}
              liveAiStatus={liveAi.status}
              liveAiMessage={liveAi.message}
              conversations={pdfConversations}
              activeThreadId={threadId}
              onNewConversation={handleNewPdfConversation}
              onSwitchConversation={handleSwitchPdfConversation}
              onDeleteConversation={handleDeletePdfConversation}
              onSendEdit={liveAi.allowed ? handleSendEditedMessage : undefined}
            />
          </div>
        </>
      )}
    </div>
  );
}

function PdfPanel({
  project,
  pdfUrl,
  onBack,
}: {
  project: Project | null;
  pdfUrl: string;
  onBack: () => void;
}) {
  return (
    <>
      <div className="flex flex-shrink-0 items-center gap-3 border-b border-gray-200 px-4 py-2.5">
        <button
          type="button"
          onClick={onBack}
          className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap text-sm text-primary-600 transition-colors hover:text-primary-800"
        >
          <HiArrowLeft className="h-4 w-4" />
          返回介紹
        </button>
        <span className="shrink-0 text-gray-300">|</span>
        <div className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium text-gray-700">{project?.title}</span>
        </div>
      </div>
      <div className="flex-1 overflow-hidden">
        {project?.pdfPath ? (
          <PdfViewer pdfUrl={pdfUrl} projectTitle={project.title} hideTitle />
        ) : (
          <div className="flex h-full items-center justify-center">
            <div className="text-center text-gray-500">
              <p className="mb-2 text-lg">目前沒有可顯示的 PDF</p>
              <p className="text-sm">這份研究計畫尚未提供 PDF 檔案。</p>
            </div>
          </div>
        )}
      </div>
    </>
  );
}


function ConversationDropdown({
  conversations,
  activeThreadId,
  onNew,
  onSwitch,
  onDelete,
}: {
  conversations: PdfConversationSummary[];
  activeThreadId: string | undefined;
  onNew: () => void;
  onSwitch: (threadId: string) => void;
  onDelete: (threadId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const activeConv = conversations.find((c) => c.thread_id === activeThreadId);
  const label = activeConv ? activeConv.title : 'AI 對話區';
  const hasConversations = conversations.length > 0 || activeThreadId !== undefined;

  if (!hasConversations) {
    return (
      <div className="min-w-0">
        <h2 className="text-sm font-semibold text-gray-800">AI 對話區</h2>
        <p className="truncate text-xs text-gray-400">可針對研究主題、內容重點與延伸問題進行提問</p>
      </div>
    );
  }

  return (
    <div ref={ref} className="relative min-w-0 flex-1">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        className="flex max-w-full items-center gap-1 rounded px-1 py-0.5 text-sm font-semibold text-gray-800 transition-colors hover:bg-gray-100"
      >
        <span className="max-w-[160px] truncate">{label}</span>
        <HiChevronDown className={`h-4 w-4 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div className="absolute left-0 top-full z-50 mt-1 w-64 overflow-hidden rounded-lg border border-gray-200 bg-white shadow-lg">
          {conversations.map((conv) => (
            <div
              key={conv.thread_id}
              className={`group flex cursor-pointer items-center justify-between px-3 py-2 hover:bg-gray-50 ${conv.thread_id === activeThreadId ? 'bg-primary-50' : ''}`}
              onClick={() => { onSwitch(conv.thread_id); setOpen(false); }}
            >
              <div className="mr-1 min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-gray-700">{conv.title}</p>
                <p className="text-xs text-gray-400">{conv.message_count} 則</p>
              </div>
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); onDelete(conv.thread_id); setOpen(false); }}
                className="shrink-0 rounded p-1 text-gray-300 opacity-0 transition-all hover:text-red-500 group-hover:opacity-100"
                aria-label="刪除對話"
              >
                <HiTrash className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
          <div className="border-t border-gray-100">
            <button
              type="button"
              onClick={() => { onNew(); setOpen(false); }}
              className="flex w-full items-center gap-2 px-3 py-2 text-sm text-primary-600 transition-colors hover:bg-primary-50"
            >
              <HiPlus className="h-4 w-4" />
              新增對話
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function ChatPanel({
  messages,
  streamingText,
  inputMessage,
  chatLoading,
  activeAgent,
  currentStage,
  onInputChange,
  onSubmit,
  onCancel,
  onClose,
  compact = false,
  isLoggedIn = true,
  onLogin,
  liveAiStatus = 'allowed',
  liveAiMessage,
  conversations = [],
  activeThreadId,
  onNewConversation,
  onSwitchConversation,
  onDeleteConversation,
  onSendEdit,
}: {
  messages: ChatMessage[];
  streamingText: string;
  inputMessage: string;
  chatLoading: boolean;
  activeAgent?: { name: string; label: string } | null;
  currentStage?: string;
  onInputChange: (value: string) => void;
  onSubmit: (event: FormEvent) => void;
  onCancel: () => void;
  onClose?: () => void;
  compact?: boolean;
  isLoggedIn?: boolean;
  onLogin?: () => void;
  liveAiStatus?: LiveAiStatus;
  liveAiMessage?: string;
  conversations?: PdfConversationSummary[];
  activeThreadId?: string;
  onNewConversation?: () => void;
  onSwitchConversation?: (threadId: string) => void;
  onDeleteConversation?: (threadId: string) => void;
  onSendEdit?: (newContent: string, fromIdx: number) => void;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const chatInputRef = useRef<HTMLTextAreaElement>(null);
  const [editingMsgIdx, setEditingMsgIdx] = useState(-1);
  const [editingContent, setEditingContent] = useState('');

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, streamingText]);

  useEffect(() => {
    const el = chatInputRef.current;
    if (!el) return;
    el.style.height = 'auto';
    const maxH = 120;
    const newH = Math.min(el.scrollHeight, maxH);
    el.style.height = `${newH}px`;
    el.style.overflowY = el.scrollHeight > maxH ? 'auto' : 'hidden';
  }, [inputMessage]);

  const handleChatKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      onSubmit(e as unknown as FormEvent);
    }
  };

  const handleSendEdit = () => {
    if (!editingContent.trim() || !onSendEdit) return;
    const idx = editingMsgIdx;
    setEditingMsgIdx(-1);
    setEditingContent('');
    onSendEdit(editingContent.trim(), idx);
  };

  const handleCancelEdit = () => {
    setEditingMsgIdx(-1);
    setEditingContent('');
  };

  const lastUserIdx = messages.reduceRight(
    (acc, m, i) => (acc === -1 && m.role === 'user' ? i : acc),
    -1,
  );

  return (
    <div className="card flex h-full flex-col overflow-hidden">
      <div className="flex flex-shrink-0 items-center justify-between gap-3 border-b border-gray-200 px-4 py-2.5">
        {onNewConversation && onSwitchConversation && onDeleteConversation ? (
          <ConversationDropdown
            conversations={conversations}
            activeThreadId={activeThreadId}
            onNew={onNewConversation}
            onSwitch={onSwitchConversation}
            onDelete={onDeleteConversation}
          />
        ) : (
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-gray-800">AI 對話區</h2>
            <p className="truncate text-xs text-gray-400">可針對研究主題、內容重點與延伸問題進行提問</p>
          </div>
        )}
        {onClose ? (
          <button
            type="button"
            onClick={onClose}
            className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-gray-200 text-gray-500 transition hover:bg-gray-50"
            aria-label="收起 AI 對話框"
          >
            <HiChevronLeft className="h-5 w-5" />
          </button>
        ) : null}
      </div>

      <div className={`flex-1 space-y-3 overflow-y-auto p-4 ${compact ? 'bg-white' : ''}`}>
        {messages.map((message, index) => {
          const isEditing = editingMsgIdx === index;
          const canEdit   = message.role === 'user' && index === lastUserIdx && !chatLoading && !!onSendEdit;
          return (
          <div key={index} className={`flex ${message.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div
              className={`${message.role === 'user' ? 'max-w-[80%]' : 'max-w-[85%]'}`}
            >
              {/* 訊息氣泡 */}
              <div
                className={`rounded-lg p-4 ${
                  message.role === 'user' ? 'bg-primary-700 text-white' : 'bg-gray-100 text-gray-900'
                }`}
                title={message.role === 'user' ? undefined : undefined}
              >
                {message.role === 'assistant' ? (
                  <div className="prose prose-sm prose-gray max-w-none text-sm">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {message.content}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <p className="whitespace-pre-wrap text-sm leading-relaxed">{message.content}</p>
                )}
              </div>

              {/* 來源頁碼（AI 回答下方；沒有 sources 就不顯示） */}
              {message.role === 'assistant' && <SourceChips sources={message.sources} />}

              {/* 編輯按鈕（氣泡下方） */}
              {canEdit && !isEditing && (
                <div className="mt-1 flex justify-end">
                  <button
                    type="button"
                    onClick={() => { setEditingMsgIdx(index); setEditingContent(message.content); }}
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
            </div>
          </div>
        );
        })}

        {chatLoading && streamingText ? (
          <div className="flex justify-start">
            <div className="max-w-[85%] rounded-lg bg-gray-100 p-4 text-gray-900">
              <div className="prose prose-sm prose-gray max-w-none text-sm">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {streamingText}
                </ReactMarkdown>
              </div>
              <span className="inline-block h-4 w-0.5 animate-pulse bg-gray-500 align-text-bottom" />
            </div>
          </div>
        ) : chatLoading ? (
          <div className="flex justify-start">
            <div className="rounded-lg bg-gray-100 p-4 text-gray-900">
              <div className="flex space-x-2">
                <div className="h-2 w-2 animate-bounce rounded-full bg-gray-400"></div>
                <div className="h-2 w-2 animate-bounce rounded-full bg-gray-400 delay-100"></div>
                <div className="h-2 w-2 animate-bounce rounded-full bg-gray-400 delay-200"></div>
              </div>
            </div>
          </div>
        ) : null}

        {chatLoading && (activeAgent || currentStage) && (
          <div className="ml-1 mt-1 flex items-center gap-1.5 text-xs text-gray-400">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-primary-500" />
            {activeAgent?.label}
            {activeAgent && currentStage && <span className="mx-0.5 text-gray-300">·</span>}
            {currentStage}
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {!isLoggedIn ? (
        <div className="border-t border-gray-200 p-4">
          <div className="flex flex-col items-center gap-3 rounded-2xl bg-slate-50 px-4 py-5 text-center">
            <div className="flex h-10 w-10 items-center justify-center rounded-full bg-slate-100">
              <HiLockClosed className="h-5 w-5 text-slate-400" />
            </div>
            <div>
              <p className="text-sm font-semibold text-slate-700">需要登入才能使用 AI 對話</p>
              <p className="mt-0.5 text-xs text-slate-400">登入後即可針對論文內容進行提問</p>
            </div>
            <button
              type="button"
              onClick={onLogin}
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
      ) : liveAiStatus === 'loading' ? (
        <LiveAiPendingSlot compact />
      ) : liveAiStatus !== 'allowed' ? (
        <LiveAiDisabledNotice
          assistant="research"
          compact={compact}
          message={liveAiMessage}
        />
      ) : (
        <form onSubmit={onSubmit} className="border-t border-gray-200 p-3">
          <div className="flex gap-2">
            <textarea
              ref={chatInputRef}
              value={inputMessage}
              onChange={(event) => onInputChange(event.target.value)}
              onKeyDown={handleChatKeyDown}
              placeholder="輸入你想了解的研究問題..."
              disabled={chatLoading}
              rows={1}
              className="min-w-0 flex-1 resize-none overflow-y-hidden rounded-md border border-gray-300 px-4 py-3 text-sm leading-6 transition-colors focus:border-primary-500 focus:ring-2 focus:ring-primary-500 disabled:bg-gray-50 disabled:text-gray-400"
            />
            {chatLoading ? (
              <button
                type="button"
                onClick={onCancel}
                className="btn-secondary shrink-0"
                aria-label="中止回應"
              >
                <HiStop className="h-5 w-5" />
              </button>
            ) : (
              <button type="submit" disabled={!inputMessage.trim()} className="btn-primary shrink-0">
                <HiPaperAirplane className="h-5 w-5" />
              </button>
            )}
          </div>
        </form>
      )}
    </div>
  );
}

function DeptSelect({
  value,
  onChange,
  departments,
  getCollegeLabel,
}: {
  value: string;
  onChange: (v: string) => void;
  departments: string[];
  getCollegeLabel: (dept: string) => string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const groups = useMemo(() => {
    const isGrad = (d: string) =>
      d.includes('研究所') || (d.includes('研究中心') && !d.includes('學系'));
    const map = new Map<string, string[]>();
    // 依照 departments 原始順序插入，保留與專題列表相同的排序
    for (const dept of departments) {
      const college = getCollegeLabel(dept);
      if (!map.has(college)) map.set(college, []);
      map.get(college)!.push(dept);
    }
    // 每個學院內：學系/學士班在前，研究所/研究中心在後
    const sorted = Array.from(map.entries()).map(([college, depts]) => [
      college,
      [
        ...depts.filter((d) => !isGrad(d)),
        ...depts.filter((d) =>  isGrad(d)),
      ],
    ] as [string, string[]]);
    // 未分類放最後；其餘保留 departments 插入順序（已由 sortDeptsByCollegeOrder 保證）
    const normal = sorted.filter(([c]) => c !== '未分類單位');
    const uncat  = sorted.filter(([c]) => c === '未分類單位');
    return [...normal, ...uncat];
  }, [departments, getCollegeLabel]);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        className="flex w-[200px] items-center gap-1 rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm focus:border-primary-500 focus:ring-2 focus:ring-primary-500"
      >
        <span className="min-w-0 flex-1 truncate text-left">{value || '全部系所'}</span>
        <HiChevronDown className={`h-4 w-4 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div className="absolute right-0 top-full z-50 mt-1 max-h-80 w-64 overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-lg">
          <div
            className={`cursor-pointer px-3 py-2 text-sm hover:bg-gray-50 ${!value ? 'font-medium text-primary-700' : 'text-gray-700'}`}
            onClick={() => { onChange(''); setOpen(false); }}
          >
            全部系所
          </div>
          <div className="my-1 border-t border-gray-100" />
          {groups.map(([college, depts], gi) => (
            <div key={college}>
              {gi > 0 && <div className="mx-3 my-0.5 border-t border-gray-50" />}
              <p className="px-3 pb-0.5 pt-2 text-[10px] font-medium tracking-wider text-gray-400">
                {college}
              </p>
              {depts.map((dept) => (
                <div
                  key={dept}
                  className={`cursor-pointer px-3 py-1.5 text-sm transition-colors hover:bg-gray-50 ${
                    dept === value ? 'bg-primary-50 font-medium text-primary-700' : 'text-gray-700'
                  }`}
                  onClick={() => { onChange(dept); setOpen(false); }}
                >
                  {dept}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function HighlightText({ text, query }: { text: string; query: string }) {
  const q = query.trim().toLowerCase();
  if (!q) return <>{text}</>;
  const idx = text.toLowerCase().indexOf(q);
  if (idx === -1) return <>{text}</>;
  return (
    <>
      {text.slice(0, idx)}
      <mark className="rounded bg-yellow-200 px-0.5 not-italic text-yellow-900">{text.slice(idx, idx + q.length)}</mark>
      {text.slice(idx + q.length)}
    </>
  );
}

function ProjectCardContent({
  project,
  getCollegeLabel,
  getCollegeBadgeClass,
  highlight = '',
}: {
  project: Project;
  getCollegeLabel: (department: string) => string;
  getCollegeBadgeClass: (college: string) => string;
  highlight?: string;
}) {
  return (
    <>
      <div className="mb-2 flex items-start justify-between">
        <span className={`badge text-xs ${getCollegeBadgeClass(getCollegeLabel(project.department))}`}>{getCollegeLabel(project.department)}</span>
        <span className="text-xs text-gray-500">{project.year}</span>
      </div>
      <h3 className="mb-2 line-clamp-2 text-sm font-semibold leading-tight text-primary-900">
        <HighlightText text={project.title} query={highlight} />
      </h3>
      <div className="space-y-1 text-xs text-gray-600">
        <div className="flex items-center">
          <HiUser className="mr-1 h-3 w-3" />
          <span>{project.studentName}</span>
        </div>
        <div className="flex items-center">
          <HiAcademicCap className="mr-1 h-3 w-3" />
          <span className="line-clamp-1">{project.department}</span>
        </div>
      </div>
    </>
  );
}

function ProjectSummary({
  project,
  getCollegeLabel,
  getCollegeBadgeClass,
}: {
  project: Project;
  getCollegeLabel: (department: string) => string;
  getCollegeBadgeClass: (college: string) => string;
}) {
  return (
    <>
      <div className="mb-6">
        <div className="mb-4 flex items-start justify-between">
          <span className={`badge ${getCollegeBadgeClass(getCollegeLabel(project.department))}`}>{getCollegeLabel(project.department)}</span>
          <span className="text-sm text-gray-500">{project.year}</span>
        </div>
        <h2 className="mb-3 text-2xl font-bold text-primary-900">{project.title}</h2>
        <div className="flex items-center gap-4 text-sm text-gray-600">
          <div className="flex items-center">
            <HiUser className="mr-1 h-4 w-4" />
            <span>{project.studentName}</span>
          </div>
          <div className="flex items-center">
            <HiAcademicCap className="mr-1 h-4 w-4" />
            <span>{project.department}</span>
          </div>
        </div>
      </div>

      <div className="mb-8 space-y-6">
        {project.motivation ? (
          <SectionBlock title="研究動機與問題">{project.motivation}</SectionBlock>
        ) : (
          <SectionBlock title="專題摘要">
            這份研究計畫來自 {project.department}，可作為了解研究主題、作品方向與成果呈現方式的參考。
          </SectionBlock>
        )}
        {project.method && <SectionBlock title="研究方法">{project.method}</SectionBlock>}
        {project.result && <SectionBlock title="研究成果">{project.result}</SectionBlock>}
        {project.tags && project.tags.length > 0 && (
          <div>
            <h3 className="mb-3 flex items-center text-lg font-semibold text-primary-900">
              <span className="mr-3 h-6 w-1 bg-primary-600"></span>
              領域標籤
            </h3>
            <div className="flex flex-wrap gap-2 pl-4">
              {project.tags.map((tag) => (
                <span key={tag} className="badge badge-primary">
                  {tag}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    </>
  );
}

function SectionBlock({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <h3 className="mb-3 flex items-center text-lg font-semibold text-primary-900">
        <span className="mr-3 h-6 w-1 bg-primary-600"></span>
        {title}
      </h3>
      <p className="pl-4 leading-relaxed text-gray-700">{children}</p>
    </div>
  );
}

function createWelcomeMessage(project: Project): ChatMessage {
  return {
    role: 'assistant',
    content: `你現在正在查看「${project.title}」這份研究計畫。如果你想了解研究方向、內容重點或延伸問題，可以直接在這裡提問。`,
  };
}

/** 把後端回傳的來源字串（"p.3"、"p.5-6"、"3"…）整理成去重後的頁碼標籤 */
function formatSourceLabels(sources?: string[]): string[] {
  if (!sources?.length) return [];
  const labels = sources
    .map((s) => (typeof s === 'string' ? s.trim() : ''))
    .filter(Boolean)
    .map((s) => (/^\d+(\s*[-–]\s*\d+)?$/.test(s) ? `p.${s.replace(/\s+/g, '')}` : s));
  return Array.from(new Set(labels));
}

function SourceChips({ sources }: { sources?: string[] }) {
  const labels = formatSourceLabels(sources);
  if (!labels.length) return null;
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-xs" aria-label="回答來源頁碼">
      <span className="text-gray-400">來源</span>
      {labels.map((label) => (
        <span
          key={label}
          className="rounded-full border border-primary-200 bg-primary-50 px-2 py-0.5 font-medium text-primary-700"
        >
          {label}
        </span>
      ))}
    </div>
  );
}
