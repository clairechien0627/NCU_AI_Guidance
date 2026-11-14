import React, { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  HiBookOpen,
  HiCalendar,
  HiChatAlt2,
  HiChevronDown,
  HiClock,
  HiDocumentText,
  HiExternalLink,
  HiFlag,
  HiLocationMarker,
  HiPaperAirplane,
  HiPlus,
  HiTag,
  HiUser,
  HiUserGroup,
  HiX,
} from 'react-icons/hi';
import { courseAPI } from '../api/services';
import { apiErrorDisplayMessage, liveAiRestrictedMessageFromError } from '../api/liveAi';
import { useLiveAiAccess } from '../hooks/useLiveAiAccess';
import type { Course } from '../types';
import CoreAbilityTable from './CoreAbilityTable';
import HighlightText from './HighlightText';
import LiveAiDisabledNotice, { LiveAiPendingSlot } from './LiveAiDisabledNotice';

interface CourseDetailPanelProps {
  course: Course | null;
  searchKeyword?: string;
  isCompared?: boolean;
  onToggleCompare?: (course: Course) => void;
}

const splitFields = (value?: string | null) =>
  (value ?? '')
    .split(/[、,;；／/]/)
    .map((item) => item.trim())
    .filter(Boolean);

const uniqueFields = (values?: string[]) =>
  Array.from(new Set((values ?? []).map((item) => item.trim()).filter(Boolean)));

// detail 可能是物件或陣列，統一轉成字串，避免把物件當成 React child 渲染而讓頁面崩潰
const getApiErrorMessage = (error: unknown) =>
  apiErrorDisplayMessage(error, 'AI 課程助理暫時無法回答，請稍後再試。');

const QUICK_ACTIONS = [
  { id: 'translate', label: '翻成中文', prompt: '請把這門課的課程目標與課程內容翻譯成繁體中文，保持原本的架構。' },
  { id: 'simplify', label: '高中生版本', prompt: '請用高中生能理解的語言，整理這門課在學什麼、有什麼實際用途。' },
  { id: 'connect', label: '連結高中知識', prompt: '這門課的核心概念可以對應到高中哪些科目的哪些單元？請舉例說明。' },
  { id: 'fit', label: '我適合修嗎', prompt: '這門課適合什麼樣背景的學生？需要哪些先備知識？有推薦先修的課程嗎？' },
];

const CourseDetailPanel: React.FC<CourseDetailPanelProps> = ({ course, searchKeyword = '', isCompared = false, onToggleCompare }) => {
  const [askOpen, setAskOpen] = useState(false);
  const [askQuestion, setAskQuestion] = useState('');
  const [askLastQuestion, setAskLastQuestion] = useState('');
  const [askAnswer, setAskAnswer] = useState('');
  const [askError, setAskError] = useState('');
  const [askLoading, setAskLoading] = useState(false);
  const [aiPanelWidth, setAiPanelWidth] = useState(360);
  const [quickActionsOpen, setQuickActionsOpen] = useState(true);
  const liveAi = useLiveAiAccess();
  const courseIdentity = course ? `${course.serial_no}-${course.course_id}` : '';

  const resetAskState = () => {
    setAskQuestion('');
    setAskLastQuestion('');
    setAskAnswer('');
    setAskError('');
    setAskLoading(false);
  };

  useEffect(() => {
    void (async () => {
      await Promise.resolve();
      setAskOpen(false);
      resetAskState();
    })();
  }, [courseIdentity]);

  const closeAsk = () => {
    setAskOpen(false);
    resetAskState();
  };

  const submitQuestion = async (question: string) => {
    if (!course || !liveAi.allowed) return;
    setAskLoading(true);
    setAskError('');
    setAskAnswer('');
    setAskLastQuestion(question);
    try {
      const response = await courseAPI.askCourse(course.course_id, question);
      setAskAnswer(response.answer);
      if (response.warnings.length > 0) {
        setAskError(response.warnings.join(' '));
      }
    } catch (error) {
      const restricted = liveAiRestrictedMessageFromError(error);
      if (restricted !== null) {
        // 後端判定目前帳號不能使用即時 AI：輸入區換成說明元件（說明裡已有同一句話，不再另外顯示錯誤）
        liveAi.markRestricted(restricted);
      } else {
        setAskError(getApiErrorMessage(error));
      }
    } finally {
      setAskLoading(false);
    }
  };

  const handleQuickAction = (prompt: string) => {
    setQuickActionsOpen(false);
    void submitQuestion(prompt);
  };

  const handleResizeDragStart = (e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = aiPanelWidth;
    const onMove = (ev: MouseEvent) => {
      setAiPanelWidth(Math.max(280, Math.min(640, startWidth + startX - ev.clientX)));
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  };

  const handleAskSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const question = askQuestion.trim();
    if (!question) {
      setAskError('請先輸入問題。');
      return;
    }
    setAskQuestion('');
    setQuickActionsOpen(false);
    await submitQuestion(question);
  };

  if (!course) {
    return (
      <div className="flex h-full items-center justify-center rounded-lg border-2 border-dashed border-slate-300 bg-slate-50">
        <div className="text-center text-gray-500">
          <HiBookOpen className="mx-auto mb-4 h-16 w-16 text-gray-400" />
          <p className="text-lg font-medium">請選擇一門課程查看詳情</p>
          <p className="mt-2 text-sm">從左側列表中選擇課程</p>
        </div>
      </div>
    );
  }

  const metadataGroups = [
    { label: '工具', items: uniqueFields(course.tools) },
    { label: '主題', items: uniqueFields(course.topic_tags) },
    { label: '領域標籤', items: uniqueFields(course.domain_tags) },
    { label: '語言', items: uniqueFields(course.languages) },
    { label: '核心問題', items: uniqueFields(course.core_questions) },
  ].filter((group) => group.items.length > 0);
  const conceptsList = uniqueFields(course.concepts);
  const simplifiedList = uniqueFields(course.simplified_concepts);
  const hasConceptData = conceptsList.length > 0 || simplifiedList.length > 0;
  const courseFieldTags = splitFields(course.course_field);

  return (
    <div className={askOpen ? 'md:flex md:items-start' : undefined}>
    <div className={`rounded-lg border border-slate-200 bg-white shadow-sm${askOpen ? ' md:flex-1 md:min-w-0' : ''}`}>
      <div className="border-b border-slate-200 bg-white p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0 flex-1">
            <div className="mb-1.5 flex flex-wrap items-center gap-2">
              <h2 className="min-w-0 text-xl font-bold leading-snug text-gray-900">
                <HighlightText text={course.course_name_zh} keyword={searchKeyword} />
              </h2>
              {courseFieldTags.map((field) => (
                <span key={field} className="rounded-full border border-indigo-100 bg-indigo-50 px-2.5 py-0.5 text-xs font-medium text-indigo-700">
                  <HighlightText text={field} keyword={searchKeyword} />
                </span>
              ))}
            </div>
            <p className="text-sm text-gray-600">
              <HighlightText text={course.course_name_en} keyword={searchKeyword} />
            </p>
            <div className="mt-4 flex flex-wrap gap-2">
              <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-sm font-medium text-slate-700">
                課號：{course.course_id || '未提供'}
              </span>
              <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-sm font-medium text-slate-700">
                {course.course_system || '未提供學制資訊'}
              </span>
              <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-sm font-medium text-slate-700">
                {course.credits} 學分
              </span>
              <span
                className={`rounded-full px-3 py-1 text-sm font-medium ${
                  (course.required_elective || '').trim() === '必修'
                    ? 'border border-red-200 bg-red-100 text-red-800'
                    : 'border border-green-200 bg-green-100 text-green-800'
                }`}
              >
                {course.required_elective || '未提供類型'}
              </span>
              <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-sm font-medium text-slate-700">
                {course.semester_display || '未提供學期'}
              </span>
            </div>
          </div>
          <div className="flex shrink-0 flex-wrap gap-2 lg:justify-end">
            <button
              type="button"
              onClick={() => {
                resetAskState();
                setAskOpen(true);
              }}
              className="inline-flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-900 px-3 py-2 text-sm font-medium text-white shadow-xl shadow-slate-300 transition duration-200 hover:-translate-y-0.5 hover:bg-slate-800 hover:shadow-2xl active:translate-y-0"
            >
              <HiChatAlt2 className="h-4 w-4" />
              詢問這門課
            </button>
            {onToggleCompare ? (
              <button
                type="button"
                onClick={() => onToggleCompare(course)}
                className={`inline-flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium shadow-sm transition ${
                  isCompared
                    ? 'border border-primary-300 bg-white text-primary-800 hover:bg-primary-50'
                    : 'border border-primary-200 bg-white/90 text-primary-800 hover:bg-white'
                }`}
              >
                {isCompared ? <HiX className="h-4 w-4" /> : <HiPlus className="h-4 w-4" />}
                {isCompared ? '移除對比' : '加入對比'}
              </button>
            ) : null}
          </div>
        </div>
      </div>

      <div className="space-y-5 p-5">
        {course.course_objective ? (
          <Section icon={<HiFlag className="h-5 w-5" />} title="課程目標">
            <p className="whitespace-pre-wrap text-sm leading-relaxed text-gray-700">
              <HighlightText text={course.course_objective} keyword={searchKeyword} />
            </p>
          </Section>
        ) : null}

        {course.course_content ? (
          <Section icon={<HiDocumentText className="h-5 w-5" />} title="課程內容">
            <p className="whitespace-pre-wrap text-sm leading-relaxed text-gray-700">
              <HighlightText text={course.course_content} keyword={searchKeyword} />
            </p>
          </Section>
        ) : null}

        {hasConceptData ? (
          <Section icon={<HiTag className="h-5 w-5" />} title="課程概念">
            <ConceptPairChips concepts={conceptsList} simplified={simplifiedList} keyword={searchKeyword} />
          </Section>
        ) : null}

        {metadataGroups.length > 0 ? (
          <Section icon={<HiTag className="h-5 w-5" />} title="課程知識標籤">
            <div className="space-y-3">
              {metadataGroups.map((group) => (
                <div key={group.label}>
                  <div className="mb-1.5 text-xs font-semibold text-gray-500">{group.label}</div>
                  <ChipList items={group.items} keyword={searchKeyword} />
                </div>
              ))}
            </div>
          </Section>
        ) : null}

        {course.core_abilities && course.core_abilities.length > 0 ? (
          <Section icon={<HiUserGroup className="h-5 w-5" />} title="核心能力">
            <CoreAbilityTable abilities={course.core_abilities} />
          </Section>
        ) : null}

        <Section icon={<HiUserGroup className="h-5 w-5" />} title="分發條件">
          {course.distribution_conditions && course.distribution_conditions.length > 0 ? (
            <div className="space-y-2">
              {course.distribution_conditions.map((condition, index) => (
                <div key={index} className="flex items-center gap-3 rounded-lg border border-primary-100 bg-primary-50 p-3">
                  <span className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-primary-500 text-sm font-medium text-white shadow-sm">
                    {condition.priority}
                  </span>
                  <p className="text-sm leading-relaxed text-gray-700">
                    <HighlightText text={condition.condition} keyword={searchKeyword} />
                  </p>
                </div>
              ))}
            </div>
          ) : (
            <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-sm leading-relaxed text-slate-600">
              沒有分發條件。
            </div>
          )}
        </Section>

        <div className="grid grid-cols-1 gap-2 border-t pt-3 md:grid-cols-2">
          <InfoItem icon={<HiUser className="h-5 w-5" />} label="授課教師">
            <HighlightText text={course.instructor || '未提供'} keyword={searchKeyword} />
          </InfoItem>
          {course.class_time ? (
            <InfoItem icon={<HiClock className="h-5 w-5" />} label="上課時間">
              {course.class_time}
            </InfoItem>
          ) : null}
          {course.classroom ? (
            <InfoItem icon={<HiLocationMarker className="h-5 w-5" />} label="上課地點">
              {course.classroom}
            </InfoItem>
          ) : null}
          {course.weeks ? (
            <InfoItem icon={<HiCalendar className="h-5 w-5" />} label="週數">
              {course.weeks} 週
            </InfoItem>
          ) : null}
          {course.teaching_method ? (
            <InfoItem icon={<HiDocumentText className="h-5 w-5" />} label="教學方式">
              {course.teaching_method}
            </InfoItem>
          ) : null}
          {course.office_hours ? (
            <InfoItem icon={<HiClock className="h-5 w-5" />} label="Office Hours">
              {course.office_hours}
            </InfoItem>
          ) : null}
        </div>

        {course.textbooks ? (
          <Section icon={<HiBookOpen className="h-5 w-5" />} title="教材與參考書">
            <p className="whitespace-pre-wrap text-sm leading-relaxed text-gray-700">
              <HighlightText text={course.textbooks} keyword={searchKeyword} />
            </p>
          </Section>
        ) : null}

        {course.grading ? (
          <Section icon={<HiDocumentText className="h-5 w-5" />} title="評分方式">
            <p className="whitespace-pre-wrap text-sm leading-relaxed text-gray-700">{course.grading}</p>
          </Section>
        ) : null}

        {course.note ? (
          <div className="rounded-lg border border-slate-200 bg-slate-50 p-4">
            <p className="text-sm text-gray-700">
              <span className="font-medium">備註：</span>
              <HighlightText text={course.note} keyword={searchKeyword} />
            </p>
          </div>
        ) : null}

        <div className="flex flex-wrap gap-2 border-t pt-3">
          {course.distribution_link ? (
            <a
              href={course.distribution_link}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-2 rounded-lg bg-primary-500 px-4 py-2 text-white transition-colors hover:bg-primary-600"
            >
              <HiExternalLink className="h-5 w-5" />
              <span>查看分發資訊</span>
            </a>
          ) : null}
          {course.outline_link ? (
            <a
              href={course.outline_link}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-2 rounded-lg bg-primary-500 px-4 py-2 text-white transition-colors hover:bg-primary-600"
            >
              <HiExternalLink className="h-5 w-5" />
              <span>查看課程大綱</span>
            </a>
          ) : null}
        </div>
      </div>
    </div>

    {/* Desktop split AI chat panel */}
    {askOpen ? (
      <>
      {/* Drag handle */}
      <div
        className="hidden md:flex w-3 self-stretch shrink-0 cursor-col-resize items-center justify-center group"
        onMouseDown={handleResizeDragStart}
      >
        <div className="h-12 w-0.5 rounded-full bg-slate-200 transition-colors group-hover:bg-primary-400" />
      </div>
      <div className="hidden md:flex shrink-0 flex-col self-start sticky top-4 max-h-[calc(100vh-6rem)] rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden" style={{ width: aiPanelWidth }}>
        <div className="flex items-center justify-between gap-3 border-b border-slate-200 px-4 py-3 shrink-0">
          <div className="flex items-center gap-2 text-sm font-semibold text-slate-700">
            <HiChatAlt2 className="h-5 w-5" />
            AI 課程助理
          </div>
          <button type="button" onClick={closeAsk} className="inline-flex h-8 w-8 items-center justify-center rounded-full bg-slate-100 text-slate-500 transition hover:bg-slate-200" aria-label="關閉">
            <HiX className="h-4 w-4" />
          </button>
        </div>
        {liveAi.allowed && (
        <div className="border-b border-slate-100 shrink-0">
          <button
            type="button"
            onClick={() => setQuickActionsOpen((v) => !v)}
            className="flex w-full items-center justify-between px-4 py-2.5 text-xs font-medium text-slate-400 transition hover:text-slate-600"
          >
            <span>快速提問</span>
            <HiChevronDown className={`h-3.5 w-3.5 transition-transform duration-200 ${quickActionsOpen ? '' : '-rotate-90'}`} />
          </button>
          {quickActionsOpen && (
            <div className="flex flex-wrap gap-1.5 px-4 pb-3">
              {QUICK_ACTIONS.map((action) => (
                <button
                  key={action.id}
                  type="button"
                  onClick={() => handleQuickAction(action.prompt)}
                  disabled={askLoading}
                  className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 text-xs font-medium text-slate-700 transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {action.label}
                </button>
              ))}
            </div>
          )}
        </div>
        )}
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 space-y-3">
          {!askLastQuestion ? (
            !liveAi.allowed ? null : (
            <div className="rounded-lg border border-slate-200 bg-white p-3 text-sm leading-relaxed text-slate-500">
              可以詢問這門課的內容、修課資格、適合背景，或使用上方快速提問。
            </div>
            )
          ) : (
            <>
              <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                <div className="mb-1 text-xs font-semibold text-slate-500">你的問題</div>
                <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-800">{askLastQuestion}</p>
              </div>
              {askLoading && (
                <div className="flex items-center gap-2 text-sm text-slate-500">
                  <div className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-slate-600" />
                  回答中...
                </div>
              )}
              {askAnswer && (
                <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                  <div className="mb-1 text-xs font-semibold text-slate-600">AI 回答</div>
                  <MarkdownAnswer text={askAnswer} />
                </div>
              )}
              {askError && (
                <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm leading-relaxed text-amber-800">
                  {askError}
                </div>
              )}
            </>
          )}
        </div>
        {liveAi.loading ? (
          <LiveAiPendingSlot compact />
        ) : !liveAi.allowed ? (
          <LiveAiDisabledNotice
            assistant="course"
            compact
            message={liveAi.message}
          />
        ) : (
        <form onSubmit={handleAskSubmit} className="border-t border-slate-200 p-3 shrink-0">
          <div className="flex gap-2">
            <textarea
              value={askQuestion}
              onChange={(event) => setAskQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault();
                  if (!askLoading && askQuestion.trim()) {
                    const q = askQuestion.trim();
                    setAskQuestion('');
                    handleQuickAction(q);
                  }
                }
              }}
              rows={2}
              placeholder="輸入問題，或點上方快速提問..."
              className="flex-1 resize-none rounded-lg border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-primary-500 focus:ring-2 focus:ring-primary-500"
              disabled={askLoading}
            />
            <button
              type="submit"
              disabled={askLoading}
              className="shrink-0 rounded-lg bg-primary-900 px-3 py-2 text-white transition hover:bg-primary-800 disabled:cursor-not-allowed disabled:bg-slate-400"
            >
              <HiPaperAirplane className="h-4 w-4" />
            </button>
          </div>
        </form>
        )}
      </div>
      </>
    ) : null}

    {/* Mobile modal */}
    {askOpen ? (
      <div className="fixed inset-0 z-50 flex items-end justify-center bg-slate-950/35 p-0 sm:items-center sm:p-4 md:hidden" onClick={closeAsk}>
        <div className="max-h-[92vh] w-full max-w-2xl overflow-hidden rounded-t-2xl bg-white shadow-2xl sm:rounded-2xl" onClick={(event) => event.stopPropagation()}>
          <div className="flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-4">
            <div className="min-w-0">
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-700">
                <HiChatAlt2 className="h-5 w-5" />
                單門課程問答
              </div>
              <h3 className="mt-1 truncate text-lg font-bold text-slate-950">{course.course_name_zh}</h3>
            </div>
            <button type="button" onClick={closeAsk} className="inline-flex h-9 w-9 items-center justify-center rounded-full bg-slate-100 text-slate-600 transition hover:bg-slate-200" aria-label="關閉單門課程問答">
              <HiX className="h-5 w-5" />
            </button>
          </div>

          {liveAi.allowed && (
          <div className="border-b border-slate-100 px-5 py-3">
            <p className="mb-2 text-xs font-medium text-slate-400">快速提問</p>
            <div className="flex flex-wrap gap-1.5">
              {QUICK_ACTIONS.map((action) => (
                <button
                  key={action.id}
                  type="button"
                  onClick={() => handleQuickAction(action.prompt)}
                  disabled={askLoading}
                  className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 text-xs font-medium text-slate-700 transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {action.label}
                </button>
              ))}
            </div>
          </div>
          )}

          <div className="max-h-[44vh] overflow-y-auto px-5 py-4">
            {askLastQuestion ? (
              <div className="mb-4 rounded-lg border border-slate-200 bg-slate-50 p-3">
                <div className="mb-1 text-xs font-semibold text-slate-500">你的問題</div>
                <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-800">{askLastQuestion}</p>
              </div>
            ) : null}

            {askAnswer ? (
              <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                <div className="mb-1 text-xs font-semibold text-slate-600">AI 回答</div>
                <MarkdownAnswer text={askAnswer} />
              </div>
            ) : liveAi.allowed ? (
              <div className="rounded-lg border border-slate-200 bg-white p-3 text-sm leading-relaxed text-slate-500">
                可以詢問這門課的內容、修課資格、適合背景、可能用到的工具或概念。回答只根據目前課程資料產生。
              </div>
            ) : null}

            {askError ? (
              <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm leading-relaxed text-amber-800">
                {askError}
              </div>
            ) : null}
          </div>

          {liveAi.loading ? (
            <LiveAiPendingSlot />
          ) : !liveAi.allowed ? (
            <LiveAiDisabledNotice
              assistant="course"
              message={liveAi.message}
            />
          ) : (
          <form onSubmit={handleAskSubmit} className="border-t border-slate-200 p-4">
            <label className="mb-2 block text-sm font-medium text-slate-700">輸入問題</label>
            <div className="flex flex-col gap-3 sm:flex-row">
              <textarea
                value={askQuestion}
                onChange={(event) => setAskQuestion(event.target.value)}
                rows={3}
                placeholder="例如：這門課需要哪些先備知識？"
                className="min-h-20 flex-1 resize-none rounded-xl border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-primary-500 focus:ring-2 focus:ring-primary-500"
                disabled={askLoading}
              />
              <button
                type="submit"
                disabled={askLoading}
                className="inline-flex items-center justify-center gap-2 rounded-xl bg-primary-900 px-4 py-3 text-sm font-medium text-white shadow-lg shadow-slate-300 transition duration-200 hover:-translate-y-0.5 hover:bg-primary-800 disabled:cursor-not-allowed disabled:translate-y-0 disabled:bg-slate-400 disabled:shadow-none"
              >
                <HiPaperAirplane className="h-4 w-4" />
                {askLoading ? '回答中' : '送出'}
              </button>
            </div>
          </form>
          )}
        </div>
      </div>
    ) : null}
    </div>
  );
};

const Section: React.FC<{ icon: React.ReactNode; title: string; children: React.ReactNode }> = ({ icon, title, children }) => (
  <div className="space-y-2">
    <div className="flex items-center gap-2 text-gray-800">
      <div className="shrink-0 text-primary-600">{icon}</div>
      <h3 className="text-base font-semibold">{title}</h3>
    </div>
    <div>{children}</div>
  </div>
);

const ChipList: React.FC<{ items: string[]; keyword: string }> = ({ items, keyword }) => {
  return (
    <div className="flex flex-wrap gap-2">
      {items.map((item) => (
        <span key={item} className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-sm font-medium text-slate-700">
          <HighlightText text={item} keyword={keyword} />
        </span>
      ))}
    </div>
  );
};

const ConceptPairChips: React.FC<{ concepts: string[]; simplified: string[]; keyword: string }> = ({ concepts, simplified, keyword }) => {
  const maxLen = Math.max(concepts.length, simplified.length);
  const pairs = Array.from({ length: maxLen }, (_, i) => ({
    concept: concepts[i] ?? '',
    simplified: simplified[i] ?? '',
  })).filter((p) => p.concept || p.simplified);

  if (pairs.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-3">
      {pairs.map((pair, i) => {
        const title = pair.concept || pair.simplified;
        const sub = pair.simplified || '';
        return (
          <div key={i} className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
            <div className="text-sm font-semibold leading-snug text-slate-800">
              <HighlightText text={title} keyword={keyword} />
            </div>
            {sub && (
              <div className="mt-1.5 text-xs leading-snug text-slate-400">
                <HighlightText text={sub} keyword={keyword} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
};

const MarkdownAnswer: React.FC<{ text: string }> = ({ text }) => (
  <div className="text-sm leading-relaxed text-slate-800">
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
        h1: ({ children }) => <h1 className="mb-2 text-base font-bold text-slate-950">{children}</h1>,
        h2: ({ children }) => <h2 className="mb-1.5 text-sm font-bold text-slate-950">{children}</h2>,
        h3: ({ children }) => <h3 className="mb-1 text-sm font-semibold text-slate-900">{children}</h3>,
        ul: ({ children }) => <ul className="mb-2 list-disc space-y-0.5 pl-5">{children}</ul>,
        ol: ({ children }) => <ol className="mb-2 list-decimal space-y-0.5 pl-5">{children}</ol>,
        li: ({ children }) => <li className="leading-relaxed">{children}</li>,
        strong: ({ children }) => <strong className="font-semibold text-slate-950">{children}</strong>,
        em: ({ children }) => <em className="italic">{children}</em>,
        a: ({ children, href }) => (
          <a href={href} target="_blank" rel="noreferrer" className="font-medium text-primary-700 underline decoration-dotted underline-offset-2 hover:text-primary-900">
            {children}
          </a>
        ),
        code: ({ children, className }) =>
          className ? (
            <code className="block overflow-x-auto rounded-lg bg-white/80 p-3 font-mono text-xs text-slate-700">
              {children}
            </code>
          ) : (
            <code className="rounded bg-white/80 px-1 py-0.5 font-mono text-xs text-slate-700">
              {children}
            </code>
          ),
        pre: ({ children }) => <pre className="mb-2 overflow-x-auto rounded-lg bg-white/80 p-3">{children}</pre>,
        blockquote: ({ children }) => (
          <blockquote className="mb-2 border-l-4 border-primary-200 pl-3 text-slate-600">
            {children}
          </blockquote>
        ),
        hr: () => <hr className="my-3 border-primary-100" />,
        table: ({ children }) => (
          <div className="mb-2 overflow-x-auto">
            <table className="w-full border-collapse text-xs">{children}</table>
          </div>
        ),
        th: ({ children }) => (
          <th className="border border-primary-100 bg-white/80 px-2 py-1 text-left font-semibold">{children}</th>
        ),
        td: ({ children }) => (
          <td className="border border-primary-100 px-2 py-1">{children}</td>
        ),
      }}
    >
      {text}
    </ReactMarkdown>
  </div>
);

const InfoItem: React.FC<{ icon: React.ReactNode; label: string; children: React.ReactNode }> = ({ icon, label, children }) => (
  <div className="flex items-start gap-2 text-sm">
    <div className="mt-0.5 text-gray-400">{icon}</div>
    <div>
      <span className="text-gray-500">{label}：</span>
      <span className="text-gray-900">{children}</span>
    </div>
  </div>
);

export default CourseDetailPanel;
