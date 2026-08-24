import React, { useEffect, useMemo, useState } from "react";
import { BookOpenText, Check, ChevronDown, ChevronRight, Loader2, Save, Sparkles } from "lucide-react";
import { api } from "../lib/api";

type ChapterDraft = {
  title?: string;
  body?: string[];
};

type DraftVersion = {
  id: string;
  label?: string;
  version_no?: number;
  char_count?: number;
  author_intent?: string;
  draft?: ChapterDraft;
  created_at?: string;
};

function visibleChars(value: string): number {
  return value.replace(/\s/g, "").length;
}

function bodyText(draft: ChapterDraft | null): string {
  return (draft?.body || []).filter(Boolean).join("\n\n");
}

function paragraphs(value: string): string[] {
  return value.split(/\n{2,}/).map(item => item.trim()).filter(Boolean);
}

export function ChapterDraftPanel({ chapterId, onUseDraft }: { chapterId: string; onUseDraft?: (text: string) => void }) {
  const [open, setOpen] = useState(true);
  const [intent, setIntent] = useState("");
  const [version, setVersion] = useState<DraftVersion | null>(null);
  const [draft, setDraft] = useState<ChapterDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const text = useMemo(() => bodyText(draft), [draft]);
  const count = useMemo(() => visibleChars(text), [text]);
  const countClass = count >= 2200 && count <= 3000 ? "ok" : "warn";

  useEffect(() => {
    let active = true;
    setVersion(null);
    setDraft(null);
    setIntent("");
    setError("");
    setNotice("");
    api<DraftVersion[]>(`/api/v1/authoring/chapters/${chapterId}/drafts`)
      .then(items => {
        if (!active || !items.length) return;
        const latest = items[0];
        setVersion(latest);
        setDraft(latest.draft || null);
        setIntent(latest.author_intent || "");
      })
      .catch(() => {
        // An empty history is a valid first-use state; generation surfaces real errors.
      });
    return () => { active = false; };
  }, [chapterId]);

  async function generate() {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api<{ version: DraftVersion }>(`/api/v1/authoring/chapters/${chapterId}/draft`, {
        method: "POST",
        body: JSON.stringify({ author_intent: intent, target_chars: 2600, client_mutation_id: crypto.randomUUID() }),
      });
      setVersion(result.version);
      setDraft(result.version.draft || null);
      setNotice("全文候选已生成，原正文没有被修改");
      setOpen(true);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "全文生成失败，原正文没有被修改");
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!draft || saving) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const result = await api<{ version: DraftVersion }>(`/api/v1/authoring/chapters/${chapterId}/drafts/save`, {
        method: "POST",
        body: JSON.stringify({ draft, base_version_id: version?.id || null }),
      });
      setVersion(result.version);
      setDraft(result.version.draft || draft);
      setNotice("人工修改已保存为新候选版本，原正文仍未修改");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "正文候选保存失败");
    } finally {
      setSaving(false);
    }
  }

  function useDraft() {
    if (!text.trim() || !onUseDraft) return;
    onUseDraft(text);
    setNotice("候选正文已填入编辑器，请人工确认后点击保存");
  }

  return (
    <section className="chapter-skeleton-panel" aria-label="章节全文候选工作区">
      <button type="button" className="chapter-skeleton-header" onClick={() => setOpen(value => !value)}>
        <span className="chapter-skeleton-title"><BookOpenText size={15} /><span><strong>AI全文候选</strong><small>AI生成 · 人工确认</small></span></span>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </button>
      {open ? (
        <div className="chapter-skeleton-body">
          <div className="chapter-skeleton-boundary"><Sparkles size={13} /> AI生成2200–3000字正文候选，不自动覆盖正文</div>
          <label className="chapter-skeleton-field">
            <span>本章灵感 / 想写什么</span>
            <textarea value={intent} onChange={event => setIntent(event.target.value)} rows={3} maxLength={6000} placeholder="例如：主角今晚必须拿到证据，现场完成一次反击，结尾让更大的麻烦找上门。" />
          </label>
          <button type="button" className="btn-primary chapter-skeleton-generate" onClick={() => void generate()} disabled={busy}>
            {busy ? <Loader2 size={14} className="nc-animate-pulse" /> : <Sparkles size={14} />}
            {busy ? "正在生成正文…" : version ? "重新生成正文" : "生成本章正文"}
          </button>
          {error ? <div className="chapter-skeleton-error" role="alert">{error}</div> : null}
          {notice ? <div className="chapter-skeleton-notice" role="status"><Check size={13} />{notice}</div> : null}
          {draft ? (
            <>
              <div className="chapter-skeleton-meta">
                <strong>{draft.title || "本章正文候选"}</strong>
                <span className={countClass}>{count}/2200–3000 字{count < 2200 ? `，还需 ${2200 - count} 字` : count > 3000 ? `，超出 ${count - 3000} 字` : "，符合范围"}</span>
              </div>
              <label className="chapter-skeleton-field">
                <span>正文候选（可人工修改）</span>
                <textarea aria-label="正文候选" value={text} onChange={event => setDraft(current => current ? { ...current, body: paragraphs(event.target.value) } : current)} rows={24} />
              </label>
              <div className="chapter-skeleton-actions">
                <button type="button" className="btn-sm btn-primary chapter-skeleton-use" onClick={useDraft} disabled={!onUseDraft || count < 2200 || count > 3000}>填入编辑器</button>
                <button type="button" className="btn-sm btn-ghost chapter-skeleton-save" onClick={() => void save()} disabled={saving || count < 2200 || count > 3000}>
                  {saving ? <Loader2 size={13} className="nc-animate-pulse" /> : <Save size={13} />}
                  {saving ? "保存中…" : "保存候选版本"}
                </button>
              </div>
              <p className="chapter-skeleton-footnote">填入编辑器只改变当前编辑区；点击编辑器的“保存”后，正文才会落库。</p>
            </>
          ) : <p className="chapter-skeleton-empty">先输入本章灵感，AI会结合人物、主线、伏笔、世界观和前文生成可直接审阅的完整正文。</p>}
        </div>
      ) : null}
    </section>
  );
}

// Keep the historical export for extensions that imported the old component;
// the editor itself now uses the full-text candidate name.
export const ChapterSkeletonPanel = ChapterDraftPanel;
