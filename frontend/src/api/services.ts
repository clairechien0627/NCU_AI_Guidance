import { apiClient } from './client';
import { getFirebaseIdToken } from '../auth/firebase';
import { liveAiRestrictedMessageFromResponse } from './liveAi';
import type {
  AssessmentMode,
  Question,
  Answer,
  AssessmentResult,
  Course,
  CourseAskResponse,
  CourseSemanticSearchParams,
  CourseSemanticSearchResponse,
  Project,
  ChatApiResponse,
  StreamEvent,
} from '../types';

const API_BASE = (import.meta.env.VITE_API_URL as string | undefined) ?? 'http://localhost:8000/api';

function getAnonSessionId(): string {
  const KEY = 'ncu_anon_session_id';
  let id = localStorage.getItem(KEY);
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem(KEY, id);
  }
  return id;
}

// 即時 AI 權限（後端依登入者 email 判斷；未登入也可呼叫）
export const liveAiAPI = {
  getAccess: async (): Promise<unknown> => {
    const response = await apiClient.get('/ai-access');
    return response.data;
  },
};

export const authAPI = {
  me: async (): Promise<{
    id: string;
    email: string;
    name: string;
    picture: string;
  }> => {
    const response = await apiClient.get('/auth/me');
    return response.data;
  },
};

// 科系兴趣量表 API
export const assessmentAPI = {
  // 後端每個大學部學系抽 1 張題卡（高一 10 張、其他模式每輪最多 16 張）；同一個 seed 會拿到同一組題目
  getQuestions: async (
    mode: AssessmentMode,
    options: { track?: 'liberal' | 'science'; subjects?: string[]; seed: number; exclude?: string[] },
  ): Promise<Question[]> => {
    const params = new URLSearchParams({ mode, seed: String(options.seed) });
    if (options.track) params.set('track', options.track);
    options.subjects?.forEach((s) => params.append('subjects', s));
    options.exclude?.forEach((d) => params.append('exclude', d)); // 再測一輪：已作答過的學系
    const response = await apiClient.get(`/assessment/questions?${params.toString()}`);
    return response.data;
  },

  submitAnswers: async (mode: AssessmentMode, answers: Answer[]): Promise<AssessmentResult> => {
    const response = await apiClient.post('/assessment/submit', { mode, answers });
    return response.data;
  },

  filterBySubjects: async (subjects: string[]): Promise<{
    departments: string[];            // 題庫系所原名（含研究所題卡）
    units?: string[];                 // 可報名的大學部學系（= question.department）
    admission_departments?: string[]; // 原始招生系所名稱
    unmatched?: string[];
    excluded?: string[];
  }> => {
    const response = await apiClient.post('/assessment/filter-by-subjects', { subjects });
    return response.data;
  },
};

// 课程 API
export const courseAPI = {
  getCourses: async (filters?: {
    college?: string;
    department?: string;
    type?: string;
    level?: 'undergrad' | 'grad';
  }): Promise<Course[]> => {
    const response = await apiClient.get('/courses', { params: filters });
    return response.data;
  },

  getCourseById: async (id: string): Promise<Course> => {
    const response = await apiClient.get(`/courses/${id}`);
    return response.data;
  },

  searchCourses: async (query: string): Promise<Course[]> => {
    const response = await apiClient.post('/course-search', { query });
    return response.data;
  },

  semanticSearch: async (params: CourseSemanticSearchParams): Promise<CourseSemanticSearchResponse> => {
    const response = await apiClient.post('/courses/semantic-search', params);
    return response.data;
  },

  askCourse: async (courseId: string, question: string): Promise<CourseAskResponse> => {
    const response = await apiClient.post(`/courses/${encodeURIComponent(courseId)}/ask`, { question });
    return response.data;
  },
};

// 聊天 API
export const chatAPI = {
  send: async (
    question: string,
    session_id?: string,
    college?: string,
    dept?: string,
  ): Promise<ChatApiResponse> => {
    const response = await apiClient.post('/chat', {
      question, session_id, college, dept,
    });
    return response.data;
  },
  clearSession: async (session_id: string): Promise<void> => {
    await apiClient.delete(`/chat/session/${session_id}`);
  },
  getSessions: async (): Promise<Array<{
    session_id: string;
    title: string;
    updated_at: string;
    turn_count: number;
  }>> => {
    const response = await apiClient.get('/chat/sessions');
    return response.data;
  },
  getSession: async (session_id: string): Promise<{
    session_id: string;
    title: string;
    updated_at: string;
    turns: Array<{
      user: string;
      assistant: string;
      course_cards: import('../types').CourseCard[];
      tools_used: string[];
      created_at: string;
    }>;
  }> => {
    const response = await apiClient.get(`/chat/session/${session_id}`);
    return response.data;
  },
};

// 串流聊天 API
export const chatStreamAPI = {
  stream(
    question: string,
    handlers: {
      onToken:       (text: string) => void;
      onToolStart:   (tool: string, args: Record<string, unknown>) => void;
      onToolDone:    (tool: string, count?: number, coursesFound?: string[], scores?: number[], scoreType?: string) => void;
      onVerifyStart: (poolSize: number) => void;
      onVerifyDone:  (selected: string[], filteredOut: string[], method?: 'tag' | 'llm') => void;
      onDone:        (event: StreamEvent) => void;
      onError:       (msg: string) => void;
      /** 後端回 403 LIVE_AI_RESTRICTED 時呼叫；沒提供就改走 onError */
      onRestricted?: (msg: string) => void;
    },
    session_id?: string,
    college?: string,
    dept?: string,
  ): AbortController {
    const controller = new AbortController();

    getFirebaseIdToken()
      .then((token) => fetch(`${API_BASE}/chat/stream`, {
      method:  'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body:    JSON.stringify({ question, session_id, college, dept }),
      signal:  controller.signal,
      }))
      .then(async (res) => {
        if (!res.ok) {
          const restricted = await liveAiRestrictedMessageFromResponse(res);
          if (restricted !== null) { (handlers.onRestricted ?? handlers.onError)(restricted); return; }
          handlers.onError(`HTTP ${res.status}`);
          return;
        }
        const reader = res.body!.getReader();
        const decoder = new TextDecoder();
        let buf = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split('\n');
          buf = lines.pop() ?? '';
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
              const ev: StreamEvent = JSON.parse(line.slice(6));
              if (ev.type === 'token')           handlers.onToken(ev.text ?? '');
              else if (ev.type === 'tool_start') handlers.onToolStart(ev.tool ?? '', ev.args ?? {});
              else if (ev.type === 'tool_done')  handlers.onToolDone(ev.tool ?? '', ev.count, ev.courses_found, ev.scores, ev.score_type);
              else if (ev.type === 'verify_start') handlers.onVerifyStart(ev.pool_size ?? 0);
              else if (ev.type === 'verify_done')  handlers.onVerifyDone(ev.selected ?? [], ev.filtered_out ?? [], ev.method);
              else if (ev.type === 'done')       handlers.onDone(ev);
              else if (ev.type === 'error')      handlers.onError(ev.message ?? '未知錯誤');
            } catch { /* malformed chunk */ }
          }
        }
      })
      .catch((err: Error) => {
        if (err.name !== 'AbortError') handlers.onError(err.message);
      });

    return controller;
  },
};

// 學習傾向分析 API
export interface AnalyticsOverview {
  total_sessions: number;
  total_turns: number;
  total_courses_explored: number;
  fav_college: string;
}

export interface AnalyticsDistItem {
  name: string;
  count: number;
  pct: number;
}

export interface AnalyticsToolItem {
  tool: string;
  label: string;
  count: number;
}

export interface AnalyticsCourseItem {
  name: string;
  count: number;
}

export interface AnalyticsGeneralEdu {
  total: number;
  categories: { name: string; count: number }[];
  top_topic_tags: { tag: string; count: number }[];
}

export interface AnalyticsData {
  overview: AnalyticsOverview;
  dept_distribution: AnalyticsDistItem[];
  college_distribution: AnalyticsDistItem[];
  tool_usage: AnalyticsToolItem[];
  top_courses: AnalyticsCourseItem[];
  top_domain_tags: { tag: string; count: number }[];
  general_edu: AnalyticsGeneralEdu;
  top_course_domains: { domain: string; count: number }[];
}

export const analyticsAPI = {
  get: async (): Promise<AnalyticsData> => {
    const response = await apiClient.get('/chat/analytics');
    return response.data;
  },
};

// 研究計畫個人分析
export interface PdfAnalyticsOverview {
  total_conversations: number;
  total_questions: number;
  total_documents_explored: number;
  avg_depth: number;
}

export interface PdfDocumentStat {
  document_id: number;
  title: string;
  department: string;
  college: string;
  year: string;
  conversation_count: number;
  question_count: number;
  avg_depth: number;
  last_viewed_at: string | null;
}

export interface PdfAnalyticsData {
  overview: PdfAnalyticsOverview;
  dept_distribution: AnalyticsDistItem[];
  college_distribution: AnalyticsDistItem[];
  depth_distribution: { range: string; count: number }[];
  exploration_type: { type: string; desc: string };
  document_list: PdfDocumentStat[];
  recent_questions: { question: string; document_title: string; created_at: string | null }[];
}

export const projectAnalyticsAPI = {
  get: async (): Promise<PdfAnalyticsData> => {
    const response = await apiClient.get('/projects/analytics');
    return response.data;
  },
};

// 監控 API（開發人員專用）
export interface MonitorHealth {
  postgres: 'ok' | 'error';
  qdrant: 'ok' | 'error';
}

export interface QdrantDebug {
  configured: boolean;
  url: string;
  api_key_set: boolean;
  status: 'ok' | 'error' | 'unknown';
  error: string | null;
  collections: string[];
}

export interface MonitorTrend {
  current: number;
  previous: number;
  change_pct: number | null;
}

export interface LatencyEndpoint {
  endpoint: string;
  count: number;
  avg_ms: number;
  p95_ms: number;
  error_count: number;
  error_rate_pct: number;
}

export interface MonitorAllTime {
  total_users: number;
  total_sessions: number;
  total_turns: number;
  total_input: number;
  total_output: number;
  online_now: number;
  estimated_cost_usd: number;
}

export interface LLMLatency {
  count: number;
  avg_ms: number;
  p95_ms: number;
  p99_ms: number;
  min_ms: number;
  max_ms: number;
}

export interface PdfAllTimeStats {
  total_users: number;
  total_sessions: number;
  total_turns: number;
  total_input: number;
  total_output: number;
  total_router_input: number;
  total_router_output: number;
  online_now: number;
  estimated_cost_usd: number;
}

export interface DepthStats {
  count: number; min: number; q1: number; median: number; q3: number; max: number; avg: number;
}

export interface PdfPeriodStats {
  active_users: number;
  new_users: number;
  sessions: number;
  turns: number;
  avg_turns_per_session: number;
  depth_stats: DepthStats | null;
  input_tokens: number;
  output_tokens: number;
  router_input_tokens: number;
  router_output_tokens: number;
  estimated_cost_usd: number;
  estimated_cost_breakdown: { agent_usd: number; router_usd: number };
  llm_latency: LLMLatency | null;
  llm_latency_trend: { date?: string; hour?: number; avg_ms: number }[];
  agent_distribution: { agent_name: string; cnt: number }[];
  latency_by_agent: { agent_name: string; cnt: number; avg_ms: number; p95_ms: number }[];
}

export interface PdfStats {
  all_time: PdfAllTimeStats;
  period: PdfPeriodStats;
  trends: {
    turns: MonitorTrend;
    tokens: MonitorTrend;
    active_users: MonitorTrend;
  };
  daily_trend: {
    date?: string;
    hour?: number;
    input: number;
    output: number;
    router_input: number;
    router_output: number;
    turns: number;
    sessions: number;
  }[];
  activity_trend: { date: string; sessions: number; turns: number }[];
  peak_hours: { hour: number; turns: number }[];
}

export interface CourseStats {
  all_time: MonitorAllTime;
  period: MonitorPeriod;
  trends: {
    turns: MonitorTrend;
    tokens: MonitorTrend;
    active_users: MonitorTrend;
  };
  peak_hours: { hour: number; turns: number }[];
  daily_trend: { date?: string; hour?: number; input: number; output: number; turns: number; sessions: number }[];
  tools_usage: { tool: string; label: string; count: number }[];
  activity_trend: { date: string; sessions: number; turns: number }[];
}

export interface CombinedStats {
  total_users: number;
  total_turns: number;
  total_cost_usd: number;
  online_now: number;
}

export interface MonitorPeriod {
  active_users: number;
  new_users: number;
  sessions: number;
  turns: number;
  avg_turns_per_session: number;
  depth_stats: DepthStats | null;
  input_tokens: number;
  output_tokens: number;
  estimated_cost_usd: number;
  llm_latency: LLMLatency | null;
  llm_latency_trend: { date?: string; hour?: number; avg_ms: number }[];
}

export interface DBStats {
  infra: {
    server_uptime_seconds: number;
    neon_region: string;
    qdrant_region: string;
    qdrant_total_points: number;
  };
  postgres: {
    connections: number;
    cache_hit_pct: number;
    db_size: string;
    tables: { name: string; rows: number; size_bytes: number; size_pretty: string }[];
  };
  qdrant: {
    collections: {
      name: string;
      points_count: number;
      segments_count: number;
      optimizer_ok: boolean;
      status: string;
      error?: string;
    }[];
  };
  qdrant_debug?: QdrantDebug;
  cloudinary: {
    total_resources: number;
    storage_bytes: number;
    storage_pretty: string;
    bandwidth_bytes: number;
    bandwidth_pretty: string;
    plan: string;
  } | null;
  cloudinary_error: string | null;
}

export interface MonitorStats {
  date_range: { start: string; end: string };
  model_name: string;
  server_uptime_seconds: number;
  system_health: MonitorHealth;
  qdrant_debug?: QdrantDebug;
  all_time: MonitorAllTime;
  period: MonitorPeriod;
  trends: {
    turns: MonitorTrend;
    tokens: MonitorTrend;
    active_users: MonitorTrend;
  };
  peak_hours: { hour: number; turns: number }[];
  daily_trend: { date?: string; hour?: number; input: number; output: number; turns: number; sessions: number }[];
  tools_usage: { tool: string; label: string; count: number }[];
  activity_trend: { date: string; sessions: number; turns: number }[];
  latency_stats: LatencyEndpoint[];
  pdf_model_name?: string;
  pdf_router_model_name?: string;
  course?: CourseStats;
  pdf?: PdfStats;
  combined?: CombinedStats;
}

export const monitorAPI = {
  getStats: async (opts: {
    preset?: '1d' | '7d' | '30d';
    start?: string;
    end?: string;
    feature?: 'all' | 'course' | 'pdf';
  } = {}): Promise<MonitorStats> => {
    const response = await apiClient.get('/monitor/stats', { params: opts });
    return response.data;
  },
  getDbStats: async (): Promise<DBStats> => {
    const response = await apiClient.get('/monitor/db-stats');
    return response.data;
  },
};

export interface PdfConversationSummary {
  thread_id: string;
  title: string;
  message_count: number;
  created_at: string | null;
}

// 大专生计划 API
export const projectAPI = {
  getProjects: async (filters?: {
    department?: string;
    year?: string;
  }): Promise<Project[]> => {
    const response = await apiClient.get('/projects', { params: filters });
    return response.data;
  },

  getProjectById: async (id: string): Promise<Project> => {
    const response = await apiClient.get(`/projects/${id}`);
    return response.data;
  },

  chatWithProject: async (projectId: string, message: string): Promise<string> => {
    const response = await apiClient.post(`/projects/${projectId}/chat`, { message });
    return response.data.reply;
  },

  streamChat(
    projectId: string,
    message: string,
    handlers: {
      onToken: (text: string) => void;
      onDone: (sessionId: string, cancelled?: boolean, sources?: string[]) => void;
      onError: (msg: string) => void;
      onReplace?: (text: string) => void;
      onSessionId?: (sessionId: string) => void;
      onAgentStart?: (agent: string, label: string) => void;
      onStage?: (text: string) => void;
      /** 後端回 403 LIVE_AI_RESTRICTED 時呼叫；沒提供就改走 onError */
      onRestricted?: (msg: string) => void;
    },
    threadId?: string,
  ): AbortController {
    const controller = new AbortController();
    const anonId = getAnonSessionId();

    getFirebaseIdToken()
      .then((token) =>
        fetch(`${API_BASE}/projects/${projectId}/chat/stream`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
            'X-Anon-Session': anonId,
          },
          body: JSON.stringify({ message, thread_id: threadId ?? null }),
          signal: controller.signal,
        }),
      )
      .then(async (res) => {
        if (!res.ok) {
          const restricted = await liveAiRestrictedMessageFromResponse(res);
          if (restricted !== null) { (handlers.onRestricted ?? handlers.onError)(restricted); return; }
          handlers.onError(`HTTP ${res.status}`);
          return;
        }
        const reader = res.body!.getReader();
        const decoder = new TextDecoder();
        let buf = '';

        let parseErrorStreak = 0;
        let aborted = false;
        let receivedDone = false;
        const processLine = (line: string) => {
          if (!line.startsWith('data: ')) return;
          try {
            const ev = JSON.parse(line.slice(6)) as {
              token?: string;
              done?: boolean;
              session_id?: string;
              cancelled?: boolean;
              heartbeat?: boolean;
              error?: string;
              replace?: string;
              sources?: string[];
              type?: string;
              agent?: string;
              label?: string;
              text?: string;
            };
            parseErrorStreak = 0;
            if (ev.heartbeat) return;
            if (ev.type === 'agent_start') { handlers.onAgentStart?.(ev.agent ?? '', ev.label ?? ''); return; }
            if (ev.type === 'stage')       { handlers.onStage?.(ev.text ?? ''); return; }
            if (ev.error) { handlers.onError(ev.error); aborted = true; return; }
            if (ev.replace !== undefined) { handlers.onReplace?.(ev.replace); return; }
            // session_id-only event (no token/done): front-load the session id so cancel works from turn 1.
            if (ev.session_id && !ev.done && ev.token === undefined) { handlers.onSessionId?.(ev.session_id); return; }
            if (ev.token !== undefined) handlers.onToken(ev.token);
            if (ev.done) { handlers.onDone(ev.session_id ?? '', ev.cancelled, ev.sources ?? []); receivedDone = true; }
          } catch (e) {
            console.error('[SSE] parse error on line:', line.slice(6, 120), e);
            parseErrorStreak += 1;
            if (parseErrorStreak >= 3) {
              handlers.onError('串流格式異常，請重新整理後再試。');
              aborted = true;
            }
          }
        };

        while (true) {
          if (aborted) { reader.cancel(); break; }
          const { done, value } = await reader.read();
          if (done) {
            buf += decoder.decode(); // flush multi-byte sequences
            break;
          }
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split('\n');
          buf = lines.pop() ?? '';
          for (const line of lines) { processLine(line); if (aborted) break; }
        }
        // flush any remaining data not ending with \n (only if not aborted)
        if (!aborted) {
          for (const line of buf.split('\n')) processLine(line);
          if (!receivedDone) {
            handlers.onError('連線中斷，請重新整理後再試。');
          }
        }
      })
      .catch((err: Error) => {
        if (err.name !== 'AbortError') handlers.onError(err.message);
      });

    return controller;
  },

  cancelChat: async (projectId: string, threadId: string): Promise<void> => {
    const anonId = getAnonSessionId();
    await apiClient.post(
      `/projects/${projectId}/chat/${threadId}/cancel`,
      undefined,
      { headers: { 'X-Anon-Session': anonId } },
    );
  },

  listConversations: async (projectId: string): Promise<PdfConversationSummary[]> => {
    const response = await apiClient.get(`/projects/${projectId}/conversations`);
    return response.data;
  },

  loadConversationMessages: async (
    projectId: string,
    threadId: string,
  ): Promise<{ role: 'user' | 'assistant'; content: string; sources?: string[] }[]> => {
    const response = await apiClient.get(`/projects/${projectId}/conversations/${threadId}/messages`);
    return response.data;
  },

  deleteConversation: async (projectId: string, threadId: string): Promise<void> => {
    await apiClient.delete(`/projects/${projectId}/conversations/${threadId}`);
  },

  listRecentChats: async (): Promise<{
    document_id: number;
    thread_id: string;
    title: string;
    created_at: string | null;
  }[]> => {
    const response = await apiClient.get('/projects/recent-chats');
    return response.data;
  },
};
