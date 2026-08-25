import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Progress } from "./Progress";

vi.mock("../lib/api", () => ({
  ApiError: class extends Error {},
  ApiEnvelope: class {},
  apiRaw: vi.fn().mockResolvedValue(undefined),
}));

const novelStub = { id: "novel-1", title: "测试小说" } as any;

afterEach(() => {
  vi.clearAllMocks();
  cleanup();
});

describe("创作进度门禁", () => {
  it("没有运行时展示真实空状态", () => {
    render(<Progress run={null} novel={null} onConfirm={vi.fn()} onRegenerateTitles={vi.fn()} onNewRun={vi.fn()} />);

    expect(screen.getByText("还没有正在运行的创作。")).toBeTruthy();
    expect(screen.queryByText("预计完成")).toBeNull();
  });

  it("合并展示 V6/V7 历史，并能打开对应记录", async () => {
    const onOpenHistory = vi.fn().mockResolvedValue(undefined);
    render(
      <Progress
        run={null}
        novel={null}
        historyTotal={2}
        history={[
          {
            id: "v7-run-1",
            project_id: "project-1",
            novel_id: "novel-1",
            novel_title: "天命债主",
            engine: "v7",
            run_type: "chapter_generation",
            status: "completed",
            chapter_number: 3,
            step_count: 8,
            total_tokens: 1200,
            total_cost: 0.12,
            created_at: "2026-08-03T10:00:00Z",
            updated_at: "2026-08-03T10:01:00Z",
          },
          {
            id: "v6-run-1",
            project_id: "project-1",
            novel_id: "novel-1",
            novel_title: "天命债主",
            engine: "v6",
            run_type: "bootstrap",
            status: "succeeded",
            chapter_number: null,
            step_count: 12,
            total_tokens: null,
            total_cost: null,
            created_at: "2026-08-03T09:00:00Z",
            updated_at: "2026-08-03T09:02:00Z",
          },
        ]}
        onOpenHistory={onOpenHistory}
        onConfirm={vi.fn()}
        onRegenerateTitles={vi.fn()}
        onNewRun={vi.fn()}
      />,
    );

    expect(screen.getByText("创作历史")).toBeTruthy();
    fireEvent.click(screen.getByText("创作历史"));
    expect(screen.getByText("V7 正文链")).toBeTruthy();
    expect(screen.getByText("V6 工作流")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "打开记录" })[0]);
    await waitFor(() => expect(onOpenHistory).toHaveBeenCalledWith(expect.objectContaining({ id: "v7-run-1", engine: "v7" })));
  });

  it("历史超过首屏时提供加载更多入口", async () => {
    const onLoadMore = vi.fn().mockResolvedValue(undefined);
    render(
      <Progress
        run={null}
        novel={null}
        historyTotal={101}
        history={[{
          id: "v7-run-1",
          project_id: "project-1",
          novel_id: "novel-1",
          novel_title: "测试小说",
          engine: "v7",
          run_type: "chapter_generation",
          status: "completed",
          chapter_number: 1,
          step_count: 1,
          total_tokens: 1,
          total_cost: 0,
          created_at: null,
          updated_at: null,
        }]}
        onLoadMoreHistory={onLoadMore}
        onConfirm={vi.fn()}
        onRegenerateTitles={vi.fn()}
        onNewRun={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByText("创作历史"));
    fireEvent.click(screen.getByRole("button", { name: /加载更多历史/ }));
    await waitFor(() => expect(onLoadMore).toHaveBeenCalledTimes(1));
  });

  it("人工节点等待时必须由用户确认书名", async () => {
    const confirm = vi.fn().mockResolvedValue(undefined);
    render(
      <Progress
        run={{
          id: "run-1",
          status: "waiting_human",
          current_node_key: "human_confirm_title",
          context: { title_candidates: ["星潮未眠", "长夜有光"] },
          nodes: [{
            node_key: "human_confirm_title",
            kind: "human",
            agent: null,
            title: "选定书名",
            status: "waiting_human",
          }],
        }}
        novel={null}
        onConfirm={confirm}
        onRegenerateTitles={vi.fn()}
        onNewRun={vi.fn()}
      />,
    );

    expect(screen.getByText("确认前流程会停在这里，不会替你擅自决定。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "星潮未眠" }));
    await waitFor(() => expect(confirm).toHaveBeenCalledWith("星潮未眠"));
  });

  it("扫榜生成的书名不显示人工确认门", () => {
    render(
      <Progress
        run={{
          id: "ranking-run-1",
          status: "waiting_human",
          current_node_key: "human_confirm_title",
          context: { source_type: "ranking_topic", suggested_title: "我,神级外卖员,开局送万界订单" },
          nodes: [{
            node_key: "human_confirm_title",
            kind: "human",
            agent: null,
            title: "选定书名",
            status: "waiting_human",
          }],
        }}
        novel={null}
        onConfirm={vi.fn()}
        onRegenerateTitles={vi.fn()}
        onNewRun={vi.fn()}
      />,
    );

    expect(screen.getByText("自动应用扫榜书名")).toBeTruthy();
    expect(screen.getByText(/不再停在普通书名确认门/)).toBeTruthy();
    expect(screen.queryByText("选择小说书名")).toBeNull();
  });

  it("失败节点显示失败原因与重试按钮，且重试打到正确端点", async () => {
    const { apiRaw } = await import("../lib/api");
    render(
      <Progress
        run={{
          id: "run-2",
          status: "failed",
          current_node_key: "plan_idea",
          context: {},
          nodes: [{
            node_key: "plan_idea",
            kind: "agent",
            agent: "deepseek",
            title: "创意策划",
            status: "failed",
            error: "模型超时",
            attempt: 1,
          }],
        }}
        novel={null}
        onConfirm={vi.fn()}
        onRegenerateTitles={vi.fn()}
        onNewRun={vi.fn()}
      />,
    );

    expect(screen.getByText("执行失败")).toBeTruthy();
    expect(screen.getByText("模型超时")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /重试此步骤/ }));
    await waitFor(() =>
      expect(apiRaw).toHaveBeenCalledWith(
        "/api/v1/runs/run-2/nodes/plan_idea/retry",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("质量待重写不是可重试故障，页面不再诱导重复调用模型", () => {
    render(
      <Progress
        run={{
          id: "run-quality",
          status: "needs_review",
          current_node_key: null,
          context: {
            canonical_generation: {
              status: "needs_review",
              blocked_reason: "开场没有兑现章节承诺",
            },
          },
          nodes: [{
            node_key: "write_chapter_draft",
            kind: "agent",
            agent: "deepseek",
            title: "章节初稿",
            status: "needs_review",
            output: { retryable: false, failure_kind: "quality_contract" },
            error: "质量门未通过",
            attempt: 1,
          }],
        }}
        novel={null}
        onConfirm={vi.fn()}
        onRegenerateTitles={vi.fn()}
        onNewRun={vi.fn()}
      />,
    );

    expect(screen.getByText("开场没有兑现章节承诺")).toBeTruthy();
    expect(screen.getByText("已停止自动重试")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /重试此步骤/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /重试待处理/ })).toBeNull();
  });

  it("旧版截断误判只重试章节并明确复用已完成步骤", async () => {
    const { apiRaw } = await import("../lib/api");
    const onNewRun = vi.fn().mockResolvedValue(undefined);
    (apiRaw as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(undefined);
    const succeededNodes = Array.from({ length: 12 }, (_, index) => ({
      node_key: `plan_${index}`,
      kind: "agent",
      agent: "deepseek",
      title: `策划 ${index + 1}`,
      status: "succeeded",
    }));

    render(
      <Progress
        run={{
          id: "run-old-contract",
          status: "failed",
          current_node_key: "write_chapter_draft",
          context: {},
          nodes: [
            ...succeededNodes,
            {
              node_key: "write_chapter_draft",
              kind: "agent",
              agent: "deepseek",
              title: "章节初稿",
              status: "failed",
              error: "旧版误判",
              output: {
                retryable: false,
                failure_kind: "generation_contract",
                retry_after_fix: {
                  available: true,
                  to_version: "2.52.0",
                  reason: "旧版将字数合格且已收束的截断恢复稿误判为失败。",
                },
              },
            },
          ],
        }}
        novel={novelStub}
        onConfirm={vi.fn()}
        onRegenerateTitles={vi.fn()}
        onNewRun={onNewRun}
      />,
    );

    expect(screen.queryByRole("button", { name: /全流程重执行/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /使用修复版重试章节/ }));
    expect(screen.getByText(/复用前面已经成功的 12 个步骤/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "确认只重试本章" }));

    await waitFor(() => expect(apiRaw).toHaveBeenCalledWith(
      "/api/v1/runs/run-old-contract/nodes/write_chapter_draft/retry",
      expect.objectContaining({ method: "POST" }),
    ));
    await waitFor(() => expect(onNewRun).toHaveBeenCalledWith("run-old-contract"));
  });

  it("空状态展示开始创作按钮，点击后新建 run 并通过 onNewRun 切换", async () => {
    const { apiRaw } = await import("../lib/api");
    const onNewRun = vi.fn().mockResolvedValue(undefined);
    (apiRaw as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ code: 0, message: "ok", data: { run_id: "fresh-run" } });

    render(
      <Progress
        run={null}
        novel={novelStub}
        onConfirm={vi.fn()}
        onRegenerateTitles={vi.fn()}
        onNewRun={onNewRun}
      />,
    );

    const startBtn = screen.getByRole("button", { name: /开始创作/ });
    fireEvent.click(startBtn);
    await waitFor(() =>
      expect(apiRaw).toHaveBeenCalledWith(
        "/api/v1/novels/novel-1/bootstrap",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    await waitFor(() => expect(onNewRun).toHaveBeenCalledWith("fresh-run"));
  });

  it("全流程重执行先弹确认框，确认后新建 run 并切换（旧 run 不删除）", async () => {
    const { apiRaw } = await import("../lib/api");
    const onNewRun = vi.fn().mockResolvedValue(undefined);
    (apiRaw as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ code: 0, message: "ok", data: { run_id: "v2-run" } });

    render(
      <Progress
        run={{
          id: "run-3",
          status: "failed",
          current_node_key: "plan_idea",
          context: {},
          nodes: [{
            node_key: "plan_idea",
            kind: "agent",
            agent: "deepseek",
            title: "创意策划",
            status: "failed",
            error: "模型超时",
            attempt: 1,
          }],
        }}
        novel={novelStub}
        onConfirm={vi.fn()}
        onRegenerateTitles={vi.fn()}
        onNewRun={onNewRun}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /全流程重执行/ }));
    expect(screen.getByText("确认全流程重执行？")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /确认重执行/ }));
    await waitFor(() =>
      expect(apiRaw).toHaveBeenCalledWith(
        "/api/v1/novels/novel-1/bootstrap",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    await waitFor(() => expect(onNewRun).toHaveBeenCalledWith("v2-run"));
  });

  it("启动/重启按钮调用 restart 端点（同一 run 内重跑）", async () => {
    const { apiRaw } = await import("../lib/api");
    (apiRaw as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(undefined);

    render(
      <Progress
        run={{
          id: "run-4",
          status: "failed",
          current_node_key: "plan_idea",
          context: {},
          nodes: [{
            node_key: "plan_idea",
            kind: "agent",
            agent: "deepseek",
            title: "创意策划",
            status: "failed",
            error: "模型超时",
            attempt: 1,
          }],
        }}
        novel={null}
        onConfirm={vi.fn()}
        onRegenerateTitles={vi.fn()}
        onNewRun={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /启动\/重启/ }));
    await waitFor(() =>
      expect(apiRaw).toHaveBeenCalledWith(
        "/api/v1/runs/run-4/restart",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });
});
