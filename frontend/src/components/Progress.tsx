import { useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Circle,
  Clock3,
  ChevronDown,
  Loader2,
  RefreshCw,
  SkipForward,
  Sparkles,
} from "lucide-react";
import { ApiEnvelope, ApiError, apiRaw } from "../lib/api";
import { cleanNovelTitle } from "../lib/titleDisplay";

type TipTapDoc = { type?: string; content?: Array<{ type: string; text?: string }> };
type Content = {
  id: string;
  project_id: string;
  parent_id: string | null;
  type: string;
  title: string;
  body: TipTapDoc;
  meta: Record<string, unknown>;
  status: string;
  updated_at: string;
};
type RunNode = {
  node_key: string;
  kind: string;
  agent: string | null;
  title: string;
  status: string;
  output?: Record<string, unknown>;
  error?: string | null;
  attempt?: number;
  started_at?: string | null;
  finished_at?: string | null;
};
type Run = {
  id: string;
  nodes: RunNode[];
  context: Record<string, unknown>;
  status?: string;
  current_node_key?: string | null;
};

export type GenerationHistoryItem = {
  id: string;
  project_id: string;
  novel_id: string | null;
  novel_title: string;
  engine: "v6" | "v7";
  run_type: string;
  status: string;
  chapter_number: number | null;
  step_count: number;
  total_tokens: number | null;
  total_cost: number | null;
  created_at: string | null;
  updated_at: string | null;
};

const HUMAN_NODE_KEYS = new Set(["human_confirm_title", "n2"]);
const RETRYABLE_STATUSES = new Set(["failed", "pending_provider"]);
const RESTARTABLE_RUN = new Set(["pending", "dispatch_failed", "failed", "pending_provider"]);
const PLANNING_NODES = new Set([
  "plan_idea", "plan_market_fit", "plan_story_pattern", "plan_core_gameplay",
  "plan_world_architecture", "plan_character_system", "plan_conflict_map",
  "blueprint_volume_plan", "blueprint_chapter_outline", "blueprint_scene_beat",
  "n3", "n4", "n5", "n6",
]);
const STATUS_LABELS: Record<string, string> = {
  pending: "等待中",
  queued: "已排队",
  running: "生成中",
  waiting_human: "等待确认",
  succeeded: "已完成",
  failed: "失败",
  pending_budget: "预算阻塞",
  pending_provider: "模型调用失败",
  pending_approval: "等待生成确认",
  needs_review: "质量待重写",
  skipped: "已跳过",
};
const RUN_LABELS: Record<string, string> = {
  pending: "等待开始",
  running: "创作中",
  waiting_human: "等待确认",
  succeeded: "已完成",
  failed: "需要处理",
  pending_provider: "需要处理",
  needs_review: "质量待处理",
  pending_approval: "等待生成确认",
};
const HISTORY_STATUS_LABELS: Record<string, string> = {
  ...STATUS_LABELS,
  completed: "已完成",
  cancelled: "已取消",
  paused: "已暂停",
};
const HISTORY_TYPE_LABELS: Record<string, string> = {
  bootstrap: "完整创作",
  chapter_generation: "章节生成",
  continue: "续写章节",
  rewrite: "章节重写",
  review: "质量审阅",
};

function visibleRunStatus(run: Run | null): string {
  if (!run) return "pending";
  const statuses = new Set((run.nodes || []).map(node => node.status));
  if (statuses.has("running") || statuses.has("queued")) return "running";
  if (statuses.has("pending_approval") || statuses.has("waiting_human")) return "pending_approval";
  if (statuses.has("failed") || statuses.has("pending_budget") || statuses.has("pending_provider") || statuses.has("needs_review")) return "needs_review";
  if (statuses.has("pending")) return "pending";
  return run.status || "pending";
}

function canRetryNode(node: RunNode): boolean {
  return RETRYABLE_STATUSES.has(node.status) && node.output?.retryable !== false;
}

function retryAfterFix(node: RunNode): Record<string, unknown> | null {
  const metadata = node.output?.retry_after_fix;
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)) return null;
  return (metadata as Record<string, unknown>).available === true
    ? metadata as Record<string, unknown>
    : null;
}

function formatTime(value?: string | null): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function historyTypeLabel(value: string): string {
  return HISTORY_TYPE_LABELS[value] || value.replaceAll("_", " ");
}

function historyStatusLabel(value: string): string {
  return HISTORY_STATUS_LABELS[value] || value;
}

function formatCost(value: number | null): string | null {
  if (value === null || value === undefined) return null;
  return `¥${Number(value).toFixed(4)}`;
}

function GenerationHistoryPanel({
  items,
  total,
  loading,
  error,
  onOpen,
  loadingMore,
  onLoadMore,
}: {
  items: GenerationHistoryItem[];
  total: number;
  loading: boolean;
  error: string;
  onOpen?: (item: GenerationHistoryItem) => Promise<void> | void;
  loadingMore: boolean;
  onLoadMore?: () => Promise<void> | void;
}) {
  const [openingId, setOpeningId] = useState("");

  async function open(item: GenerationHistoryItem) {
    if (!onOpen) return;
    setOpeningId(`${item.engine}:${item.id}`);
    try {
      await onOpen(item);
    } finally {
      setOpeningId("");
    }
  }

  return (
    <details className="generation-history starlume-card">
      <summary className="generation-history-head">
        <div>
          <p className="eyebrow">RUN HISTORY</p>
          <h3>创作历史</h3>
        </div>
        <div className="generation-history-summary">
          <span>{total > 0 ? `共 ${total} 条` : "暂无记录"}</span>
          <span className="generation-history-toggle">查看记录 <ChevronDown size={16} /></span>
        </div>
      </summary>

      {loading && (
        <div className="generation-history-message"><Loader2 className="spin" size={17} /> 正在加载历史记录…</div>
      )}
      {!loading && error && (
        <div className="generation-history-message error"><AlertTriangle size={17} /> {error}</div>
      )}
      {!loading && !error && items.length === 0 && (
        <div className="generation-history-message">当前项目暂无历史记录。V6/V7 的运行记录会统一显示在这里。</div>
      )}
      {!loading && !error && items.length > 0 && (
        <div className="generation-history-list">
          {items.map(item => {
            const itemKey = `${item.engine}:${item.id}`;
            const cost = formatCost(item.total_cost);
            return (
              <div className="generation-history-row" key={itemKey}>
                <div className="generation-history-main">
                  <div className="generation-history-title">
                    <span className={`generation-history-engine ${item.engine}`}>
                      {item.engine === "v7" ? "V7 正文链" : "V6 工作流"}
                    </span>
                    <strong>{cleanNovelTitle(item.novel_title, "未命名作品")}</strong>
                  </div>
                  <div className="generation-history-meta">
                    <span>{historyTypeLabel(item.run_type)}</span>
                    {item.chapter_number !== null && <span>第 {item.chapter_number} 章</span>}
                    <span className={`generation-history-status ${item.status}`}>{historyStatusLabel(item.status)}</span>
                    {item.step_count > 0 && <span>{item.step_count} 步</span>}
                    {cost && <span>{cost}</span>}
                    <span>{formatTime(item.updated_at || item.created_at)}</span>
                  </div>
                </div>
                <button
                  type="button"
                  className="btn-sm btn-ghost generation-history-open"
                  disabled={!onOpen || openingId !== "" || loadingMore}
                  onClick={() => void open(item)}
                >
                  {openingId === itemKey ? <><Loader2 className="spin" size={14} /> 正在打开…</> : "打开记录"}
                </button>
              </div>
            );
          })}
          {total > items.length && onLoadMore && (
            <button type="button" className="btn-sm btn-ghost generation-history-more" disabled={loadingMore} onClick={() => void onLoadMore()}>
              {loadingMore ? <><Loader2 className="spin" size={14} /> 正在加载更多…</> : `加载更多历史（已显示 ${items.length}/${total}）`}
            </button>
          )}
        </div>
      )}
    </details>
  );
}

function readableLabel(key: string): string {
  const labels: Record<string, string> = {
    synopsis: "简介",
    selling_points: "核心卖点",
    worldview: "世界观",
    characters: "人物卡",
    outline: "大纲",
    title: "标题",
    arc: "人物弧",
    rules: "规则",
    name: "名称",
  };
  return labels[key] || key.replaceAll("_", " ");
}

function OutputValue({ value }: { value: unknown }) {
  if (value === null || value === undefined || value === "") return <span className="muted-output">未产出</span>;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return <span>{String(value)}</span>;
  }
  if (Array.isArray(value)) {
    return value.length
      ? <ul>{value.map((item, index) => <li key={index}><OutputValue value={item} /></li>)}</ul>
      : <span className="muted-output">未产出</span>;
  }
  if (typeof value === "object") {
    return (
      <div className="output-tree">
        {Object.entries(value as Record<string, unknown>).map(([key, item]) => (
          <div key={key}><strong>{readableLabel(key)}：</strong><OutputValue value={item} /></div>
        ))}
      </div>
    );
  }
  return <span>{String(value)}</span>;
}

export function Progress({
  run,
  novel,
  onConfirm,
  onRegenerateTitles,
  onNewRun,
  onOpenWizard,
  history = [],
  historyTotal = 0,
  historyLoading = false,
  historyLoadingMore = false,
  historyError = "",
  onOpenHistory,
  onLoadMoreHistory,
}: {
  run: Run | null;
  novel: Content | null;
  onConfirm: (title: string) => Promise<void>;
  onRegenerateTitles: (feedback: string) => Promise<void>;
  onNewRun: (runId: string) => Promise<void>;
  onOpenWizard?: () => void;
  history?: GenerationHistoryItem[];
  historyTotal?: number;
  historyLoading?: boolean;
  historyLoadingMore?: boolean;
  historyError?: string;
  onOpenHistory?: (item: GenerationHistoryItem) => Promise<void> | void;
  onLoadMoreHistory?: () => Promise<void> | void;
}) {
  const nodes = run?.nodes || [];
  const currentKey = run?.current_node_key || String(run?.context?.current_node_key || "");
  const human = nodes.find(node => HUMAN_NODE_KEYS.has(node.node_key));
  const titles = Array.isArray(run?.context?.title_candidates) ? run?.context?.title_candidates as string[] : [];
  const selectedTitle = String(run?.context?.selected_title || human?.output?.selected_title || "");
  const rankingAutoTitle = human?.status === "waiting_human"
    && run?.context?.source_type === "ranking_topic"
    && Boolean(String(run?.context?.suggested_title || "").trim())
    && !selectedTitle.trim();
  const [selectedNodeKey, setSelectedNodeKey] = useState("");
  const [customTitle, setCustomTitle] = useState("");
  const [titleFeedback, setTitleFeedback] = useState("");
  const [titleBusy, setTitleBusy] = useState(false);
  const [retrying, setRetrying] = useState("");
  const [restarting, setRestarting] = useState("");
  const [bootstrapping, setBootstrapping] = useState(false);
  const [showReexecute, setShowReexecute] = useState(false);
  const [fixRetryNodeKey, setFixRetryNodeKey] = useState("");
  const [notice, setNotice] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  const selectedNode = useMemo(
    () => nodes.find(node => node.node_key === selectedNodeKey)
      || nodes.find(node => node.node_key === currentKey)
      || nodes.find(node => node.status === "running")
      || nodes[0],
    [nodes, selectedNodeKey, currentKey],
  );
  const succeededCount = nodes.filter(node => node.status === "succeeded").length;
  const percent = nodes.length ? Math.round((succeededCount / nodes.length) * 100) : 0;
  const activeNode = nodes.find(node => node.status === "running")
    || nodes.find(node => node.node_key === currentKey && ["pending", "queued"].includes(node.status));
  const runDisplayStatus = visibleRunStatus(run);
  const failedCount = nodes.filter(node => node.status === "failed" || node.status === "pending_budget" || node.status === "pending_provider" || node.status === "needs_review").length;
  const pendingApproval = nodes.some(node => node.status === "pending_approval" || node.status === "waiting_human") || run?.status === "pending_approval";
  const canonicalGeneration = (run?.context?.canonical_generation || {}) as Record<string, unknown>;
  const canonicalStatus = String(canonicalGeneration.status || run?.context?.canonical_generation_status || "");
  const generationQuality = (canonicalGeneration.generation_quality || {}) as Record<string, unknown>;
  const generationFailures = Array.isArray(generationQuality.failures)
    ? generationQuality.failures as Array<Record<string, unknown>>
    : [];
  const rootGenerationFailures = generationFailures.some(item => String(item.code || "") !== "generation_preflight_failed")
    ? generationFailures.filter(item => String(item.code || "") !== "generation_preflight_failed")
    : generationFailures;
  const generationFailureReason = rootGenerationFailures
    .map(item => String(item.message || item.code || ""))
    .filter(Boolean)
    .slice(0, 2)
    .join("；");
  const canonicalReason = String(
    canonicalGeneration.blocked_reason
      || canonicalGeneration.reason
      || generationFailureReason
      || "",
  );
  const canonicalScore = canonicalGeneration.review_score;
  const qualityGate = (canonicalGeneration.quality_gate || {}) as Record<string, unknown>;
  const rawQualityFailures = (qualityGate.failures || []) as Array<Record<string, unknown>>;
  const qualityFailures = rawQualityFailures.some(item => String(item.dimension || item.code || "") !== "generation_preflight_failed")
    ? rawQualityFailures.filter(item => String(item.dimension || item.code || "") !== "generation_preflight_failed")
    : rawQualityFailures;
  const canonicalNeedsReview = canonicalStatus === "needs_review"
    || canonicalStatus === "needs_rewrite"
    || run?.status === "needs_review"
    || generationQuality.passed === false;
  const planningNodes = nodes.filter(node => PLANNING_NODES.has(node.node_key));
  const retryableNodes = nodes.filter(canRetryNode);
  const retryAfterFixNodes = nodes.filter(node => retryAfterFix(node) !== null);
  const fixRetryNode = nodes.find(node => node.node_key === fixRetryNodeKey) || null;
  const canRestartRun = RESTARTABLE_RUN.has(run?.status || "")
    && (["pending", "dispatch_failed"].includes(run?.status || "") || retryableNodes.length > 0);
  const novelName = cleanNovelTitle(novel?.title || selectedTitle || titles[0]);

  async function retry(node: RunNode, afterFix = false) {
    if (!run) return;
    setRetrying(node.node_key);
    setNotice(null);
    try {
      await apiRaw(`/api/v1/runs/${run.id}/nodes/${node.node_key}/retry`, { method: "POST", body: "{}" });
      setFixRetryNodeKey("");
      await onNewRun(run.id);
      setNotice({
        kind: "success",
        text: afterFix
          ? `“${node.title}”已使用修复版本重新排队；前面已完成的策划步骤不会重跑。`
          : `“${node.title}”已重新排队，页面会自动刷新状态。`,
      });
    } catch (caught) {
      const detail = caught instanceof ApiError ? caught.message : String(caught);
      setNotice({ kind: "error", text: `重试失败：${detail}` });
    } finally {
      setRetrying("");
    }
  }

  async function confirmTitle(title: string) {
    if (!title.trim()) return;
    setTitleBusy(true);
    setNotice(null);
    try {
      await onConfirm(title.trim());
      setCustomTitle("");
      setNotice({ kind: "success", text: `已确认书名《${title.trim()}》，创作流程将继续。` });
    } catch (caught) {
      setNotice({ kind: "error", text: `书名确认失败：${caught instanceof Error ? caught.message : String(caught)}` });
    } finally {
      setTitleBusy(false);
    }
  }

  async function regenerateTitles() {
    setTitleBusy(true);
    setNotice(null);
    try {
      await onRegenerateTitles(titleFeedback.trim());
      setTitleFeedback("");
      setNotice({ kind: "success", text: "新的书名候选已生成，请继续选择。" });
    } catch (caught) {
      setNotice({ kind: "error", text: `重新生成失败：${caught instanceof Error ? caught.message : String(caught)}` });
    } finally {
      setTitleBusy(false);
    }
  }

  async function reexecuteAll() {
    if (!novel?.id) return;
    setBootstrapping(true);
    setNotice(null);
    try {
      const envelope = await apiRaw<ApiEnvelope<{ run_id: string }>>(
        `/api/v1/novels/${novel.id}/bootstrap`,
        { method: "POST", body: "{}" },
      );
      const newRunId = envelope?.data?.run_id;
      setShowReexecute(false);
      if (!newRunId) {
        setNotice({ kind: "error", text: "全流程重执行已提交，但未返回新的 run_id。" });
        return;
      }
      await onNewRun(newRunId);
      setNotice({ kind: "success", text: "已新建一次完整创作 run，旧 run 与章节、版本均保留。" });
    } catch (caught) {
      const detail = caught instanceof ApiError ? caught.message : String(caught);
      setNotice({ kind: "error", text: `全流程重执行失败：${detail}` });
    } finally {
      setBootstrapping(false);
    }
  }

  async function restartCurrentRun() {
    if (!run) return;
    setRestarting(run.id);
    setNotice(null);
    try {
      await apiRaw(`/api/v1/runs/${run.id}/restart`, { method: "POST", body: "{}" });
      setNotice({ kind: "success", text: "已在原 run 内重启未完成步骤，run 与章节、版本保持不变。" });
      await onNewRun(run.id);
    } catch (caught) {
      const detail = caught instanceof ApiError ? caught.message : String(caught);
      setNotice({ kind: "error", text: `重启失败：${detail}` });
    } finally {
      setRestarting("");
    }
  }

  if (!run) {
    return (
      <div className="progress-page page-enter">
        <section className="progress-empty">
          <span><Sparkles size={25} /></span>
          <p className="eyebrow">CREATION PROGRESS</p>
          <h2>还没有正在运行的创作。</h2>
          <p>从「创作向导」启动一本小说后，AI 的每一步真实状态、产物和失败原因都会显示在这里。</p>
          <div className="progress-empty-actions">
          {novel?.id ? (
            <button className="btn-sm btn-primary" disabled={bootstrapping} onClick={() => void reexecuteAll()}>
              <Sparkles size={15} /> {bootstrapping ? "正在启动…" : "开始创作"}
            </button>
          ) : onOpenWizard ? (
            <button className="btn-sm btn-primary" onClick={onOpenWizard}><Sparkles size={15} /> 打开创作向导</button>
          ) : null}
          </div>
        </section>
        <GenerationHistoryPanel items={history} total={historyTotal} loading={historyLoading} error={historyError} onOpen={onOpenHistory} loadingMore={historyLoadingMore} onLoadMore={onLoadMoreHistory} />
      </div>
    );
  }

  return (
    <div className="progress-page page-enter">
      <section className="progress-heading">
        <div>
          <p className="eyebrow">CREATION PROGRESS</p>
          <h2>{novelName}</h2>
          <p>
            {activeNode ? `正在执行：${activeNode.title}` : pendingApproval ? "正文生成等待确认，尚未写入最终章节。" : canonicalNeedsReview ? "正文草稿已保存，但质量门未通过，等待重写。" : failedCount ? "流程遇到问题，请查看失败步骤。" : runDisplayStatus === "succeeded" ? "策划与首章生成已经完成。" : "等待流程继续。"}
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <span className={`progress-run-state ${runDisplayStatus}`}>{RUN_LABELS[runDisplayStatus] || runDisplayStatus}</span>
          {retryableNodes.length > 0 && (
            <button className="btn-sm btn-primary" disabled={retrying !== ""} onClick={() => {
              void retry(retryableNodes[0]);
            }}><RefreshCw size={14} /> 重试可恢复故障 ({retryableNodes.length})</button>
          )}
          {retryAfterFixNodes.length > 0 && (
            <button className="btn-sm btn-primary" disabled={retrying !== ""} onClick={() => {
              setFixRetryNodeKey(retryAfterFixNodes[0].node_key);
            }}><RefreshCw size={14} /> 使用修复版重试章节 ({retryAfterFixNodes.length})</button>
          )}
          {canRestartRun && (
            <button className="btn-sm btn-ghost" disabled={restarting !== ""} onClick={() => void restartCurrentRun()}>
              <RefreshCw size={14} /> 启动/重启
            </button>
          )}
          {nodes.length > 0 && run.status !== "running" && retryAfterFixNodes.length === 0 && (
            <button className="btn-sm btn-primary" disabled={bootstrapping} onClick={() => setShowReexecute(true)}>
              <RefreshCw size={14} /> 全流程重执行
            </button>
          )}
        </div>
      </section>

      {notice && <div className={`progress-notice ${notice.kind}`} role="status">{notice.kind === "error" ? <AlertTriangle size={17} /> : <CheckCircle2 size={17} />}{notice.text}</div>}

      <GenerationHistoryPanel items={history} total={historyTotal} loading={historyLoading} error={historyError} onOpen={onOpenHistory} loadingMore={historyLoadingMore} onLoadMore={onLoadMoreHistory} />

      {(pendingApproval || canonicalNeedsReview || canonicalReason) && (
        <section className={`progress-notice ${canonicalNeedsReview ? "error" : "success"}`} role="status">
          {canonicalNeedsReview ? <AlertTriangle size={17} /> : <Clock3 size={17} />}
          <div>
            <strong>{pendingApproval ? "生成尚未完成" : "生成结果需要处理"}</strong>
            <span>
              {canonicalReason || (canonicalNeedsReview ? "质量门未通过，草稿已保存为待重写。" : "系统正在等待生成确认。")}
            </span>
            {canonicalScore !== undefined && canonicalScore !== null && (
              <small style={{ display: "block", marginTop: 6, opacity: 0.78 }}>
                审阅参考分：{String(canonicalScore)}。参考分不替代字数、开场、连续性等硬约束。
              </small>
            )}
            {canonicalNeedsReview && qualityFailures.length > 0 && (
              <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px solid rgba(255,255,255,0.1)" }}>
                <div style={{ fontWeight: 600, marginBottom: 6, fontSize: 13 }}>失败原因：</div>
                <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.6 }}>
                  {qualityFailures.slice(0, 5).map((failure, idx) => {
                    const dim = String(failure.dimension || "unknown");
                    const reason = String(failure.reason || "");
                    const actual = failure.actual;
                    const minimum = failure.minimum;
                    let displayText = reason || dim;
                    if (dim === "overall_score") {
                      displayText = `质量分不足：${actual} 分（阈值 ${minimum} 分）`;
                    } else if (dim === "sensitive_content" || dim === "content_policy") {
                      displayText = `敏感内容检测未通过：${reason || "命中敏感词"}`;
                    } else if (dim === "blocking_violations") {
                      displayText = `严重违规：${actual} 项（阈值 0 项）`;
                    } else if (dim === "payoff_contract") {
                      displayText = `爽点契约未完成：${reason}`;
                    } else if (dim === "third_person_narrative") {
                      displayText = `人称问题：${reason}`;
                    } else if (dim === "ai_pattern_risk") {
                      displayText = `AI 腔风险过高：${reason}`;
                    } else if (dim === "duplicate_paragraph") {
                      displayText = `段落重复：${reason}`;
                    } else if (reason) {
                      displayText = `${dim}：${reason}`;
                    }
                    return <li key={idx}>{displayText}</li>;
                  })}
                  {qualityFailures.length > 5 && (
                    <li style={{ opacity: 0.7 }}>... 还有 {qualityFailures.length - 5} 项问题</li>
                  )}
                </ul>
              </div>
            )}
          </div>
        </section>
      )}

      <section className="progress-overview starlume-card">
        <div className="progress-number">
          <strong>{percent}%</strong>
          <span>整体完成度</span>
        </div>
        <div className="progress-track-wrap">
          <div><span>{succeededCount} / {nodes.length} 个步骤完成</span><span>{failedCount ? `${failedCount} 个步骤需处理` : "状态自动保存"}</span></div>
          <div className="progress-track" aria-label={`创作流程完成 ${percent}%`}><span style={{ width: `${percent}%` }} /></div>
        </div>
        <div className="progress-fact">
          <span>当前步骤</span>
          <strong>{rankingAutoTitle ? "自动应用扫榜书名" : (activeNode?.title || (human?.status === "waiting_human" ? "确认书名" : "—"))}</strong>
        </div>
      </section>

      {rankingAutoTitle && (
        <section className="title-gate starlume-card">
          <div className="title-gate-heading">
            <span><CheckCircle2 size={20} /></span>
            <div>
              <p className="eyebrow">扫榜书名</p>
              <h3>已自动应用《{String(run?.context?.suggested_title || "").trim()}》</h3>
              <p>该书名来自扫榜选题，创作流程会自动继续，不再停在普通书名确认门。</p>
            </div>
          </div>
        </section>
      )}

      {human?.status === "waiting_human" && !rankingAutoTitle && (
        <section className="title-gate starlume-card">
          <div className="title-gate-heading">
            <span><Sparkles size={20} /></span>
            <div><p className="eyebrow">需要你的决定</p><h3>选择小说书名</h3><p>确认前流程会停在这里，不会替你擅自决定。</p></div>
          </div>
          {titles.length ? (
            <div className="title-candidate-grid">
              {titles.map(title => (
                <button type="button" key={title} disabled={titleBusy} onClick={() => void confirmTitle(title)}>
                  <span>{title}</span><Check size={16} />
                </button>
              ))}
            </div>
          ) : <div className="title-loading"><Loader2 className="spin" size={18} /> 等待书名候选写入…</div>}
          <div className="title-custom-grid">
            <label>
              <span>使用自己的书名</span>
              <div><input maxLength={120} placeholder="输入你确定的书名" value={customTitle} onChange={event => setCustomTitle(event.target.value)} /><button type="button" disabled={titleBusy || !customTitle.trim()} onClick={() => void confirmTitle(customTitle)}>确认使用</button></div>
            </label>
            <label>
              <span>让 AI 重新生成</span>
              <div><input maxLength={500} placeholder="可选：告诉 AI 想强调什么" value={titleFeedback} onChange={event => setTitleFeedback(event.target.value)} /><button type="button" disabled={titleBusy} onClick={() => void regenerateTitles()}>{titleBusy ? "生成中…" : "重新生成"}</button></div>
            </label>
          </div>
        </section>
      )}

      <section className="progress-workspace">
        <div className="node-list starlume-card">
          <div className="node-list-heading"><h3>创作步骤</h3><span>{nodes.length} 个真实节点</span></div>
          {nodes.map((node, index) => {
            const active = selectedNode?.node_key === node.node_key;
            return (
              <button type="button" key={node.node_key} className={`${active ? "active" : ""} ${node.status}`} onClick={() => setSelectedNodeKey(node.node_key)} title={node.status === "skipped" ? "此步骤由 V7 引擎内部完成，无需单独执行" : undefined}>
                <span className="node-order">
                  {node.status === "succeeded" ? <Check size={15} /> : node.status === "running" ? <Loader2 className="spin" size={15} /> : node.status === "failed" || node.status === "pending_budget" || node.status === "pending_provider" || node.status === "needs_review" ? <AlertTriangle size={15} /> : node.status === "pending_approval" || node.status === "waiting_human" ? <Clock3 size={15} /> : node.status === "skipped" ? <SkipForward size={13} /> : <Circle size={13} />}
                </span>
                <span className="node-name"><strong>{node.title}</strong><small>{STATUS_LABELS[node.status] || node.status}</small></span>
                <span className="node-index">{String(index + 1).padStart(2, "0")}</span>
              </button>
            );
          })}
        </div>

        <article className="node-detail starlume-card">
          {selectedNode ? (
            <>
              <div className="node-detail-heading">
                <div><p className="eyebrow">{selectedNode.node_key}</p><h3>{selectedNode.title}</h3></div>
                <span className={`node-status ${selectedNode.status}`}>{STATUS_LABELS[selectedNode.status] || selectedNode.status}</span>
              </div>
              {selectedNode.status === "skipped" && (
                <div style={{ marginBottom: 12, padding: "10px 12px", borderRadius: 8, background: "var(--bg-subtle, #f5f5f7)", fontSize: 13, color: "var(--text-secondary, #6b7280)", display: "flex", gap: 8, alignItems: "flex-start" }}>
                  <SkipForward size={16} style={{ flexShrink: 0, marginTop: 1 }} />
                  <span>此步骤由 V7 引擎内部完成，无需单独执行。V7 高级生成引擎已在章节生成过程中内置了质量审查、去AI味、连续性检查等功能。</span>
                </div>
              )}
              <div className="node-meta">
                <span><Clock3 size={14} /> 开始 {formatTime(selectedNode.started_at)}</span>
                <span>完成 {formatTime(selectedNode.finished_at)}</span>
                <span>尝试 {selectedNode.attempt || 0} 次</span>
              </div>
              {selectedNode.error && <div className="node-error"><AlertTriangle size={17} /><div><strong>执行失败</strong><p>{selectedNode.error}</p></div></div>}
              {selectedNode.output?.retryable === false && retryAfterFix(selectedNode) === null && (
                <div className="node-error" style={{ marginTop: 10 }}>
                  <AlertTriangle size={17} />
                  <div><strong>已停止自动重试</strong><p>这是确定性质量或生成契约问题；请先修正页面列出的原因，再重新生成。</p></div>
                </div>
              )}
              {retryAfterFix(selectedNode) && (
                <div className="node-error" style={{ marginTop: 10 }}>
                  <RefreshCw size={17} />
                  <div>
                    <strong>新版已修复这个旧误判</strong>
                    <p>{String(retryAfterFix(selectedNode)?.reason || "可只重试当前章节，前面已完成的策划步骤会保留。")}</p>
                  </div>
                </div>
              )}
              {canRetryNode(selectedNode) && (
                <button type="button" className="retry-node" disabled={Boolean(retrying)} onClick={() => void retry(selectedNode)}>
                  {retrying === selectedNode.node_key ? <><Loader2 className="spin" size={16} /> 正在重新排队…</> : <><RefreshCw size={16} /> 重试此步骤</>}
                </button>
              )}
              <div className="node-output">
                <h4>节点产物</h4>
                {selectedNode.output && Object.keys(selectedNode.output).length
                  ? <OutputValue value={selectedNode.output} />
                  : <p className="muted-output">该步骤尚未产出内容。这里不会显示模拟结果。</p>}
              </div>
            </>
          ) : <p className="muted-output">暂无可查看步骤。</p>}
        </article>
      </section>

      {planningNodes.length > 0 && (
        <section className="planning-section">
          <div className="section-heading"><div><p className="eyebrow">STORY BLUEPRINT</p><h3>策划产物</h3></div></div>
          <div className="planning-grid">
            {planningNodes.map(node => (
              <details className="starlume-card" key={node.node_key}>
                <summary><span>{node.title}</span><small>{STATUS_LABELS[node.status] || node.status}</small></summary>
                <div>{node.output && Object.keys(node.output).length ? <OutputValue value={node.output} /> : <p className="muted-output">尚未产出</p>}</div>
              </details>
            ))}
          </div>
        </section>
      )}

      {showReexecute && (
        <div className="modal-backdrop" role="dialog" aria-modal="true" onClick={() => { if (!bootstrapping) setShowReexecute(false); }}>
          <div className="modal-card" onClick={event => event.stopPropagation()}>
            <h3>确认全流程重执行？</h3>
            <p>这会<strong>新建一次</strong>完整创作 run，从策划到首章重新生成。当前的 run、已生成的章节与版本都<strong>不会被删除</strong>，你可以在进度页切换查看历史记录。</p>
            <div className="modal-actions">
              <button type="button" className="btn-sm btn-ghost" disabled={bootstrapping} onClick={() => setShowReexecute(false)}>取消</button>
              <button type="button" className="btn-sm btn-primary" disabled={bootstrapping} onClick={() => void reexecuteAll()}>
                {bootstrapping ? "正在新建…" : "确认重执行"}
              </button>
            </div>
          </div>
        </div>
      )}

      {fixRetryNode && (
        <div className="modal-backdrop" role="dialog" aria-modal="true" onClick={() => { if (!retrying) setFixRetryNodeKey(""); }}>
          <div className="modal-card" onClick={event => event.stopPropagation()}>
            <h3>使用修复版本重试章节？</h3>
            <p>
              只会重新执行失败的<strong>“{fixRetryNode.title}”</strong>，复用前面已经成功的 {succeededCount} 个步骤；
              不会重跑策划，也不会删除旧失败记录。确认后将调用一次真实 Provider，并继续使用最多两次候选的章节生成上限。
            </p>
            <div className="modal-actions">
              <button type="button" className="btn-sm btn-ghost" disabled={Boolean(retrying)} onClick={() => setFixRetryNodeKey("")}>取消</button>
              <button type="button" className="btn-sm btn-primary" disabled={Boolean(retrying)} onClick={() => void retry(fixRetryNode, true)}>
                {retrying ? "正在重新排队…" : "确认只重试本章"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
