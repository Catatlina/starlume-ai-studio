import React, { useEffect, useMemo, useRef, useState } from "react";
import { Check, FilePenLine, Save, RotateCcw, Wand2, RefreshCcw, X, ChevronLeft, ChevronRight, BookOpenText } from "lucide-react";
import { RichEditor } from "./RichEditor";
import { EditorAiChat } from "./EditorAiChat";
import { PacingCurve } from "./PacingCurve";
import { SceneBoard } from "./SceneBoard";
import { EditorContextPanel } from "./EditorContextPanel";
import { EditorRoleStatus } from "./EditorRoleStatus";
import { ChapterDraftPanel } from "./ChapterSkeletonPanel";
import { Pagination } from "./ui";
import { usePagination } from "../hooks/usePagination";
import "../styles/novel-prose.css";

type Content = { id: string; title: string; body: { content?: { text?: string }[] }; meta: Record<string, unknown>; parent_id?: string | null };
type Version = { id: string; label: string; reason?: string; snapshot: Record<string, unknown>; created_at: string };
type PendingAiEdit = { op: string; originalText: string; proposedText: string; nextText: string };

function readableIssue(value: unknown): string {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return String(value ?? "");
  const item = value as Record<string, unknown>;
  const description = item.description || item.issue || item.message || item.reason || item.title;
  const suggestion = item.suggestion || item.recommendation || item.fix;
  if (description && suggestion) return `${String(description)}（建议：${String(suggestion)}）`;
  if (description) return String(description);
  return Object.entries(item).filter(([, entry]) => entry !== null && entry !== undefined && entry !== "")
    .map(([key, entry]) => `${key}：${typeof entry === "object" ? JSON.stringify(entry) : String(entry)}`)
    .join("；");
}

/** V7 is canonical; ``score`` remains a compatibility alias for old payloads. */
export function formatLiveReviewScore(review: Record<string, unknown> | null | undefined): string {
  if (!review) return "--";
  for (const candidate of [review.overall_score, review.score, review.review_score, review.self_score]) {
    const numeric = typeof candidate === "number" ? candidate : Number(candidate);
    if (Number.isFinite(numeric)) {
      return Number.isInteger(numeric) ? String(numeric) : numeric.toFixed(1);
    }
  }
  return "--";
}

const EDITOR_OPERATION_LABELS: Record<string, string> = {
  continue: "续写",
  polish: "润色",
  rewrite: "改写",
  rewrite_chapter: "整章重写",
  deai: "去 AI 味",
};

export function Editor({ chapter, chapters, selectChapter, editorText, setEditorText, selection, setSelection, saveChapter, runEditorOp, versions, restoreVersion, offlineNotice, offlineQueueCount, offlineAiResults, applyOfflineAiResult, streamPreview, liveReviewing, liveReviewError, onRequestReview, editorAiReview, pendingAiEdit, applyPendingAiEdit, discardPendingAiEdit, deaiResult, deaiLoading, markLiked, projectId, editorResetNonce, editorAiLoading, editorAiOperation }: {
  chapter: Content | null; chapters: Content[]; selectChapter: (id: string) => void;
  editorText: string; setEditorText: (t: string) => void;
  selection: string; setSelection: (s: string) => void;
  saveChapter: (textOverride?: string) => void | Promise<boolean>; runEditorOp: (op: string, instruction?: string, targetText?: string) => Promise<{ text?: string } | null | undefined>;
  versions: Version[]; restoreVersion: (id: string) => void;
  offlineNotice?: string; offlineQueueCount?: number;
  offlineAiResults?: Array<{ id: string; text: string }>;
  applyOfflineAiResult?: (id: string, text: string) => void;
  streamPreview?: string;
  editorAiReview?: { review?: any; next?: any } | null;
  liveReviewError?: string;
  pendingAiEdit?: PendingAiEdit | null;
  applyPendingAiEdit?: () => Promise<void>;
  discardPendingAiEdit?: () => void;
  deaiResult?: { original_score?: number; final_score?: number; layers?: Array<{ name: string; label: string; score_before: number; score_after: number; status: string }>; final_text?: string } | null;
  deaiLoading?: boolean;
  markLiked?: (text: string) => void;
  projectId?: string;
  liveReviewing?: boolean;
  editorResetNonce?: number;
  editorAiLoading?: boolean;
  editorAiOperation?: string;
  onRequestReview?: () => void;
}) {
  const conflict = versions.find(version => version.label === "offline_conflict" && version.reason === "offline_conflict");
  const docText = (body: any) => body?.content?.map((item: any) => item?.text || "").join("\n\n") || "";
  const localConflictText = useMemo(() => docText((conflict?.snapshot as any)?.body), [conflict?.id]);
  const serverText = useMemo(() => docText(chapter?.body), [chapter?.id, (chapter as any)?.updated_at]);
  const [mergeText, setMergeText] = useState("");
  const [conflictDismissed, setConflictDismissed] = useState(false);
  useEffect(() => {
    setMergeText(localConflictText || editorText);
    setConflictDismissed(false);
  }, [conflict?.id]);

  // ── UI state toggles ──
  const [isFullscreen, setFullscreen] = useState(false);
  const [isNightMode, setNightMode] = useState(false);
  const [isFocusMode, setFocusMode] = useState(false);

  // ── 章节重写状态 ──
  // ── Keyboard shortcut: Escape to exit fullscreen ──
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && isFullscreen) setFullscreen(false);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [isFullscreen]);

  // NC-LIB-003: debounced autosave
  const saveRef = useRef(saveChapter);
  saveRef.current = saveChapter;
  const [autoSavedAt, setAutoSavedAt] = useState("");
  const dirty = !!chapter && editorText !== serverText;
  const readVisibleEditorText = () => {
    const editorNode = Array.from(document.querySelectorAll<HTMLElement>(".ed-main .ProseMirror"))
      .find(node => node.isConnected && (node.offsetParent !== null || node.getClientRects().length > 0));
    if (!editorNode) return editorText;
    return editorNode.innerText || editorNode.textContent || "";
  };
  useEffect(() => {
    const handler = async (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "s") return;
      event.preventDefault();
      if (editorAiLoading || !chapter) return;
      const saved = await Promise.resolve(saveRef.current(readVisibleEditorText()));
      if (saved !== false) setAutoSavedAt(new Date().toLocaleTimeString());
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [chapter?.id, editorAiLoading]);
  useEffect(() => {
    if (!chapter || !dirty) return;
    if (conflict && !conflictDismissed) return;
    const timer = setTimeout(() => {
      saveRef.current();
      setAutoSavedAt(new Date().toLocaleTimeString());
    }, 3000);
    return () => clearTimeout(timer);
  }, [editorText, chapter?.id, dirty, conflict?.id, conflictDismissed]);

  // ── Chapter tree from chapters list ──
  const chapterTree = useMemo(() => {
    return chapters.map((ch, i) => ({
      id: ch.id,
      title: ch.title,
      seq: Number(ch.meta?.seq || i + 1),
    }));
  }, [chapters]);

  // Paginate the outline / offline-result lists.
  const chapterTreePager = usePagination({ items: chapterTree, pageSize: 10, mode: "client" });
  const offlineResultsPager = usePagination({ items: offlineAiResults ?? [], pageSize: 10, mode: "client" });
  const currentChapterIndex = chapter ? chapterTree.findIndex(item => item.id === chapter.id) : -1;
  const previousChapter = currentChapterIndex > 0 ? chapterTree[currentChapterIndex - 1] : null;
  const nextChapter = currentChapterIndex >= 0 && currentChapterIndex < chapterTree.length - 1
    ? chapterTree[currentChapterIndex + 1]
    : null;
  const [chapterNavigationBusy, setChapterNavigationBusy] = useState(false);

  async function moveToChapter(chapterId: string) {
    if (!chapter || chapterId === chapter.id || chapterNavigationBusy) return;
    setChapterNavigationBusy(true);
    try {
      if (dirty) {
        const saved = await Promise.resolve(saveChapter());
        if (saved === false) return;
      }
      setSelection("");
      selectChapter(chapterId);
    } finally {
      setChapterNavigationBusy(false);
    }
  }

  // ── Word count ──
  const wordCount = useMemo(() => {
    const text = editorText.replace(/<[^>]*>/g, "");
    return text.replace(/\s/g, "").length;
  }, [editorText]);

  if (!chapter) {
    return (
      <section className="editor-empty">
        <span><FilePenLine size={25} /></span>
        <p className="eyebrow">CHAPTER EDITOR</p>
        <h2>还没有可以编辑的章节。</h2>
        <p>先从创作向导生成首章，或在书库中打开一本已有章节的小说。这里不会创建一段假的示例正文。</p>
      </section>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", background: "var(--bg-base)" }}>
      {/* ── Minimal toolbar ── */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "8px 0", borderBottom: "1px solid var(--border-subtle)", marginBottom: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>{chapter ? chapter.title : "编辑器"}</span>
          <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
            {chapter ? `字数 ${wordCount.toLocaleString()}` : ""}
            {dirty && <span style={{ color: "var(--warning)", marginLeft: 8 }}>未保存</span>}
          </span>
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <span className="editor-author-led-badge"><BookOpenText size={13} />AI辅助创作 · 全文候选待人工确认</span>
          <button className="btn-sm btn-ghost" disabled={editorAiLoading || !selection.trim()} onClick={() => runEditorOp("polish")} style={{ gap: 4 }}>
            <Wand2 size={13} />选区润色
          </button>
          <button onClick={() => void saveChapter(readVisibleEditorText())} disabled={!chapter || editorAiLoading} className="btn-sm btn-primary" style={{ gap: 4 }}>
            <Save size={14} />保存
          </button>
        </div>
      </div>

      {editorAiLoading ? (
        <div className="ai-msg" role="status" style={{ marginBottom: 12 }}>
          <RefreshCcw size={14} style={{ animation: "spin 1s linear infinite", marginRight: 8 }} />
          AI 正在处理“{EDITOR_OPERATION_LABELS[editorAiOperation || ""] || editorAiOperation || "当前操作"}”，原文未改变；最多等待 2 分钟。
        </div>
      ) : null}

      {/* ── Stream preview ── */}
      {streamPreview ? (
        <div className="ai-stream-preview">
          <small><span className="spinner" /> AI 正在生成预览，正文尚未改变</small>
          <div style={{ marginTop: 4 }}>{streamPreview}</div>
        </div>
      ) : null}

      {pendingAiEdit ? (
        <section className="ai-edit-preview" aria-live="polite">
          <div className="ai-edit-preview-head">
            <div><p className="eyebrow">PREVIEW BEFORE APPLY</p><h3>AI 建议已生成，等待你的确认</h3></div>
            <span>{pendingAiEdit.op}</span>
          </div>
          <div className="ai-edit-compare">
            <label><span>原文</span><textarea readOnly value={pendingAiEdit.originalText || "（追加操作，无替换原文）"} /></label>
            <label><span>AI 建议</span><textarea readOnly value={pendingAiEdit.proposedText} /></label>
          </div>
          <p>应用后会更新编辑器正文并立即保存，自动创建可恢复版本；放弃则原文保持不变。</p>
          <div className="ai-edit-preview-actions">
            <button type="button" onClick={discardPendingAiEdit}><X size={16} /> 放弃建议</button>
            <button type="button" className="primary" onClick={() => void applyPendingAiEdit?.()}><Check size={16} /> 应用到编辑器并保存</button>
          </div>
        </section>
      ) : null}

      {/* ── Offline notice ── */}
      {(offlineNotice || offlineQueueCount) ? (
        <div className="badge orange" style={{ marginBottom: 12, padding: "8px 14px", fontSize: 12 }}>
          {offlineNotice || `离线队列 ${offlineQueueCount} 项`}
        </div>
      ) : null}

      {/* ── Conflict resolution ── */}
      {conflict && !conflictDismissed ? (
        <div className="card" style={{ marginBottom: 12 }}>
          <h2 style={{ fontSize: 16, fontWeight: 700, color: "var(--orange)", marginBottom: 12 }}>离线版本冲突</h2>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8 }}>
            <label style={{ fontSize: 12, color: "var(--text-2)" }}>本地稿<textarea className="form-input" value={localConflictText} readOnly rows={8} style={{ marginTop: 4, background: "var(--bg-muted)", fontSize: 12 }} /></label>
            <label style={{ fontSize: 12, color: "var(--text-2)" }}>服务器稿<textarea className="form-input" value={serverText} readOnly rows={8} style={{ marginTop: 4, background: "var(--bg-muted)", fontSize: 12 }} /></label>
            <label style={{ fontSize: 12, color: "var(--text-2)" }}>合并稿<textarea className="form-input" value={mergeText} onChange={event => setMergeText(event.target.value)} rows={8} style={{ marginTop: 4, background: "var(--bg-muted)", fontSize: 12 }} /></label>
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <button className="btn-sm btn-ghost" onClick={() => { setEditorText(localConflictText); setConflictDismissed(true); }}>采用本地稿</button>
            <button className="btn-sm btn-ghost" onClick={() => { setEditorText(serverText); setConflictDismissed(true); }}>采用服务器稿</button>
            <button className="btn-sm btn-primary" style={{ width: "auto" }} onClick={() => { setEditorText(mergeText); setConflictDismissed(true); }}>采用合并稿</button>
          </div>
        </div>
      ) : null}

      {/* ── 3-column editor layout (prototype) ── */}
      <div className="editor" style={{ flex: 1, minHeight: 0 }}>
        {/* LEFT: Chapter outline */}
        <div className="ed-side">
          <div className="card-title" style={{ marginBottom: 12 }}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" width="16" height="16">
              <path d="M3 3h18v18H3z"/><path d="M9 3v18M3 9h6"/>
            </svg>
            章节目录
          </div>
          {chapterTree.length > 0 ? (
            chapterTreePager.pageData.map(ch => (
              <div
                key={ch.id}
                className={`outline-item${chapter?.id === ch.id ? " active" : ""}`}
                onClick={() => void moveToChapter(ch.id)}
                style={chapter?.id === ch.id ? { color: "var(--primary-light)", background: "var(--primary-dim)" } : {}}
              >
                <span style={{ opacity: 0.5, fontSize: 11, minWidth: 24 }}>{ch.seq}.</span>
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{ch.title}</span>
              </div>
            ))
          ) : (
            <div style={{ fontSize: 12, color: "var(--text-3)", padding: "8px 10px" }}>
              暂无章节
            </div>
          )}
          {chapterTreePager.totalPages > 1 && (
            <Pagination
              page={chapterTreePager.page}
              pageSize={chapterTreePager.pageSize}
              total={chapterTree.length}
              onPageChange={chapterTreePager.setPage}
              onPageSizeChange={chapterTreePager.setPageSize}
              pageSizeOptions={[10, 20, 50, 100]}
            />
          )}

          {/* V3 §11.2: pacing curve over persisted per-chapter rhythm scores */}
          {(chapter?.parent_id || chapters[0]?.parent_id) ? (
            <PacingCurve novelId={(chapter?.parent_id || chapters[0]?.parent_id) as string} />
          ) : null}

          {/* V3-P3-⑪: Scene Director 分镜面板 */}
          {chapter?.id ? (
            <SceneBoard chapterId={chapter.id} projectId={projectId || (chapter?.parent_id as string)} />
          ) : null}

          {/* Chapter selector dropdown (compact, for large lists) */}
          {chapters.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <select
                value={chapter?.id ?? ""}
                onChange={event => void moveToChapter(event.target.value)}
                aria-label="选择章节"
                className="form-input"
                style={{ height: 34, fontSize: 12 }}
              >
                {chapters.map((item, index) => (
                  <option key={item.id} value={item.id}>
                    {Number(item.meta?.seq || index + 1)}. {item.title}
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>

        {/* CENTER: Novel prose editor */}
        <div className="ed-main" style={{ padding: 0, display: "flex", flexDirection: "column" }}>
          <RichEditor
            key={`${chapter?.id ?? "empty"}:${(chapter as any)?.updated_at ?? ""}:${editorResetNonce ?? 0}`}
            value={editorText}
            onChange={setEditorText}
            onSelection={setSelection}
            selection={selection}
            onAiOp={(op, instruction) => runEditorOp(op, instruction)}
            aiBusy={editorAiLoading}
            aiReview={editorAiReview ?? null}
            deaiResult={deaiResult ?? null}
            deaiLoading={deaiLoading ?? false}
            autoSavedAt={autoSavedAt}
            dirty={dirty}
            isFullscreen={isFullscreen}
            isNightMode={isNightMode}
            isFocusMode={isFocusMode}
            onToggleFullscreen={() => setFullscreen(!isFullscreen)}
            onToggleNightMode={() => setNightMode(!isNightMode)}
            onToggleFocusMode={() => setFocusMode(!isFocusMode)}
            hideAiPanel={true}
          />
          <nav className="editor-chapter-nav" aria-label="章节导航">
            <button
              type="button"
              className="btn-sm btn-ghost editor-chapter-nav-button"
              disabled={!previousChapter || chapterNavigationBusy || editorAiLoading}
              onClick={() => previousChapter && void moveToChapter(previousChapter.id)}
            >
              <ChevronLeft size={15} />上一章
            </button>
            <div className="editor-chapter-position">
              <strong>第 {currentChapterIndex >= 0 ? currentChapterIndex + 1 : "-"} 章</strong>
              <span>/ {chapterTree.length || "-"}</span>
              {dirty && <small>未保存</small>}
            </div>
            <div className="editor-chapter-nav-actions">
              <button
                type="button"
                className="btn-sm btn-ghost editor-chapter-nav-button"
                disabled={!nextChapter || chapterNavigationBusy || editorAiLoading}
                title={nextChapter ? "打开下一章" : "当前已经是最新章节"}
                onClick={() => nextChapter && void moveToChapter(nextChapter.id)}
              >
                下一章<ChevronRight size={15} />
              </button>
              <span className="editor-next-chapter-note">
                {nextChapter ? "下一章已建立，可继续编辑" : "下一章由你决定，先在 AI 共创助手里讨论"}
              </span>
            </div>
          </nav>
        </div>

        {/* RIGHT: AI editing conversation */}
        <div className="ed-aside" style={{ display: "flex", flexDirection: "column" }}>
          <ChapterDraftPanel chapterId={chapter.id} onUseDraft={setEditorText} />
          <EditorContextPanel chapterId={chapter.id} />
          <EditorRoleStatus projectId={projectId} />
          <div className="editor-rail-heading" style={{ marginBottom: 12 }}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" width="16" height="16">
              <rect x="3" y="11" width="18" height="10" rx="2"/>
              <circle cx="12" cy="5" r="2"/>
            </svg>
            <div>
              <strong>AI 共创助手</strong>
              <span>先讨论，再生成候选；正文由你确认</span>
            </div>
          </div>

          <EditorAiChat
            chapterId={chapter.id}
            selection={selection}
            busy={editorAiLoading}
            suggestions={(editorAiReview?.review?.issues || []).map(readableIssue).filter(Boolean)}
            onRequestEdit={(instruction, targetText) => {
              if (targetText) {
                return runEditorOp("rewrite", instruction, targetText);
              } else {
                return runEditorOp("rewrite_chapter", instruction);
              }
            }}
          />

          <div className="editor-ai-review-feed">

            {liveReviewing && !editorAiReview?.review ? (
              <div className="ai-msg" role="status">
                <RefreshCcw size={14} style={{ animation: "spin 1s linear infinite", marginRight: 8 }} />
                实时审计中…审计完成后会在这里显示评分、问题和可执行建议。
              </div>
            ) : null}

            {liveReviewError ? (
              <div className="ai-msg" role="alert" style={{ borderColor: "var(--warning, #d97706)" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                  <span>{liveReviewError}</span>
                  <button type="button" className="btn-sm btn-ghost" onClick={() => onRequestReview?.()} disabled={liveReviewing}>
                    <RefreshCcw size={12} />重试审计
                  </button>
                </div>
              </div>
            ) : null}

            {editorAiReview?.review ? (
              <div className="ai-msg">
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8, flexWrap: "wrap", gap: 6 }}>
                  <strong>
                    实时审阅
                    {liveReviewing && (
                      <span className="badge" style={{ marginLeft: 6 }}>
                        <RefreshCcw size={11} style={{ animation: "spin 1s linear infinite", marginRight: 4 }} />审计中…
                      </span>
                    )}
                  </strong>
                  <span style={{ display: "flex", gap: 6, alignItems: "center" }}>
                    <span className="badge">评分：{formatLiveReviewScore(editorAiReview.review)}</span>
                    <button type="button" className="btn-sm btn-ghost" onClick={() => onRequestReview?.()} disabled={liveReviewing}>
                      <RefreshCcw size={12} />重新审计
                    </button>
                  </span>
                </div>
                {editorAiReview.review.quality_gate && !editorAiReview.review.quality_gate.passed ? (
                  <div style={{ marginBottom: 12, padding: "8px 10px", borderRadius: 8, background: "var(--warning-dim, #fff7e6)", color: "var(--warning, #a15c00)", fontSize: 12 }}>
                    当前文本仍需定向修复：{(editorAiReview.review.quality_gate.quality_repair_contract?.required_repairs || [])
                      .map((item: any) => item.label)
                      .filter(Boolean)
                      .join("、") || "存在未解决的质量问题"}。修复后请重新审计，不能只靠平均分通过。
                  </div>
                ) : null}
                {editorAiReview.review.issue_evidence?.suppressed > 0 ? (
                  <div style={{ marginBottom: 10, color: "var(--text-muted, #8b8b93)", fontSize: 12 }}>
                    已隐藏 {editorAiReview.review.issue_evidence.suppressed} 条缺少正文定位证据的泛化建议；只保留能在当前正文中找到依据的问题。
                  </div>
                ) : null}
                {editorAiReview.review.issues?.length ? (
                  <>
                    {editorAiReview.review.issues.map((issue: unknown, index: number) => (
                      <div key={`${readableIssue(issue)}-${index}`} style={{ marginBottom: 8 }}>
                        <div>• {readableIssue(issue)}</div>
                      </div>
                    ))}
                  </>
                ) : (
                  <div style={{ fontSize: 13, color: "var(--text-2)" }}>✅ 当前文本暂无审阅问题</div>
                )}
              </div>
            ) : null}

            {/* De-AI results display */}
            {deaiResult && (
              <div className="ai-msg" style={{ borderColor: "var(--primary)", background: "var(--primary-dim)" }}>
                <strong style={{ color: "var(--primary-light)" }}>去AI味完成</strong>
                <div style={{ marginTop: 4, fontSize: 12 }}>
                  原始: {deaiResult.original_score ?? "--"}分 → 最终: {deaiResult.final_score ?? "--"}分
                </div>
                {(deaiResult.layers || []).map((layer: any, i: number) => (
                  <div key={i} style={{ fontSize: 11, marginTop: 2, color: "var(--text-2)" }}>
                    {layer.label}: {layer.score_before} → {layer.score_after} ({layer.status === "pass" ? "✓" : "—"})
                  </div>
                ))}
              </div>
            )}

            {deaiLoading && (
              <div className="ai-msg" style={{ color: "var(--text-2)" }}>
                <RefreshCcw size={14} style={{ animation: "spin 1s linear infinite", marginRight: 8 }} />
                去AI味处理中…
              </div>
            )}
          </div>

        </div>
      </div>

      {/* ── Bottom: Version history ── */}
      {versions.length > 0 && (() => {
        const sorted = [...versions].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
        const latest = sorted[0];
        const cnLabel: Record<string, string> = {
          before_restore: "回滚前备份",
          ai_edit: "AI 编辑",
          offline_save: "自动保存",
          offline_conflict: "离线冲突",
          initial_idea: "初始构思",
          manual_save: "手动保存",
          ai_generate: "AI 生成",
        };
        const labelText = cnLabel[latest.label] || latest.label;
        return (
          <div className="card" style={{ marginTop: 12, padding: "14px 18px" }}>
            <div className="card-title" style={{ marginBottom: 10 }}>
              版本历史
            </div>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
              <div style={{ fontSize: 13 }}>
                <span className="badge gray" style={{ marginRight: 8 }}>{labelText}</span>
                <small style={{ color: "var(--text-3)" }}>{new Date(latest.created_at).toLocaleString()}</small>
              </div>
              <button type="button" onClick={() => restoreVersion(latest.id)} className="btn-sm btn-primary" style={{ fontSize: 12 }}>
                <RotateCcw size={12} /> 回滚到此版本
              </button>
            </div>
            {offlineAiResults?.length ? (
              <>
                <div className="card-title" style={{ marginTop: 12, marginBottom: 8 }}>离线 AI 结果</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {offlineResultsPager.pageData.map(result => (
                    <button key={result.id} onClick={() => applyOfflineAiResult?.(result.id, result.text)} className="btn-sm btn-ghost" style={{ fontSize: 12 }}>
                      应用 AI 结果
                      <small style={{ color: "var(--text-3)", marginLeft: 4, maxWidth: 120, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{result.text.slice(0, 36)}…</small>
                    </button>
                  ))}
                </div>
                {offlineResultsPager.totalPages > 1 && (
                  <Pagination
                    page={offlineResultsPager.page}
                    pageSize={offlineResultsPager.pageSize}
                    total={offlineAiResults?.length ?? 0}
                    onPageChange={offlineResultsPager.setPage}
                    onPageSizeChange={offlineResultsPager.setPageSize}
                    pageSizeOptions={[10, 20, 50, 100]}
                  />
                )}
              </>
            ) : null}
          </div>
        );
      })()}

      {/* ── Fullscreen overlay ── */}
      {isFullscreen && (
        <div style={{
          position: "fixed", inset: 0, zIndex: 200,
          background: "var(--bg)", padding: 16,
          display: "flex", flexDirection: "column"
        }}>
          <RichEditor
            key={`${chapter?.id ?? "empty"}:${(chapter as any)?.updated_at ?? ""}:${editorResetNonce ?? 0}:fullscreen`}
            value={editorText}
            onChange={setEditorText}
            onSelection={setSelection}
            selection={selection}
            onAiOp={(op: string, instruction?: string) => runEditorOp(op, instruction)}
            aiReview={editorAiReview ?? null}
            deaiResult={deaiResult ?? null}
            deaiLoading={deaiLoading ?? false}
            autoSavedAt={autoSavedAt}
            dirty={dirty}
            isFullscreen={true}
            isNightMode={isNightMode}
            isFocusMode={false}
            onToggleFullscreen={() => setFullscreen(false)}
            onToggleNightMode={() => setNightMode(!isNightMode)}
            onToggleFocusMode={() => setFocusMode(!isFocusMode)}
            hideAiPanel={true}
          />
        </div>
      )}

    </div>
  );
}
