import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { ChapterDraftPanel } from "./ChapterSkeletonPanel";

vi.mock("../lib/api", () => ({ api: vi.fn() }));

afterEach(cleanup);

describe("编辑器全文候选", () => {
  beforeEach(() => {
    vi.mocked(api).mockImplementation(async (path: string, init?: RequestInit) => {
      if (init?.method === "POST" && path.endsWith("/draft")) {
        return {
          version: {
            id: "draft-1",
            draft: { title: "门后的账本", body: ["正文".repeat(1200)] },
          },
        } as never;
      }
      return [] as never;
    });
  });

  it("请求2200-3000字全文候选，并可填入编辑器而不自动保存", async () => {
    const onUseDraft = vi.fn();
    render(<ChapterDraftPanel chapterId="chapter-1" onUseDraft={onUseDraft} />);

    fireEvent.change(screen.getByRole("textbox", { name: "本章灵感 / 想写什么" }), {
      target: { value: "本章要在现场完成一次反击" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成本章正文" }));

    await waitFor(() => expect(screen.getByRole("textbox", { name: "正文候选" })).toBeTruthy());
    const call = vi.mocked(api).mock.calls.find(([, init]) => init?.method === "POST");
    expect(call?.[0]).toBe("/api/v1/authoring/chapters/chapter-1/draft");
    expect(JSON.parse(String(call?.[1]?.body)).target_chars).toBe(2600);

    fireEvent.click(screen.getByRole("button", { name: "填入编辑器" }));
    expect(onUseDraft).toHaveBeenCalledWith("正文".repeat(1200));
    expect(vi.mocked(api).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });
});
