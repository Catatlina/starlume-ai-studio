# Starlume AI 小说主线需求追踪矩阵

## 2026-08-24 全文生成候选与三章真实 Provider 证据（已部署；三章未通过）

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 编辑器按作者意图生成2200–3000字完整正文 | 可用 | `authoring.chapter_draft 1.0.0`、真实 Provider 网关、服务端2200–3000可见字门禁；生产单章 2921 字、`provider_verified=true` | 登录态浏览器、人工读感和多样本质量 |
| AI正文候选不覆盖原稿 | 可用 | 独立 `versions(entity_type=chapter_draft)`；版本 `0a083082-a0ea-483f-814b-55f1e193ccd5`，正文哈希前后相同 | 新代码登录态浏览器回归 |
| 人工修改和候选版本保存 | 已接线 | `/drafts/save` 以 `draft_human_edit` 另存版本，仍不改 `contents.body`；代码已部署 | 生产登录态人工应用与主保存 |
| 旧章节骨架历史兼容 | 可用 | 原 `/skeleton`、`/skeletons` 接口保留；主编辑器入口切换为全文候选 | 后续是否保留独立骨架 UI |
| 朱雀95/5/0与正文质量 | 未验收 | 本批只切换生成形态，没有外部检测或多样本人工验收 | 新代码真实 Provider 样本、读者校验、朱雀复验 |

### 三章真实 Provider 证据

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 全新空历史副本连续完成三章 | 未通过 | `f596d4b2...` 第1章通过后第2章开场分类误判；`734fe04e...` 第1章第3场截断；`fb13d536...` 第1章第2场被“支吾”中的“吾”误报为第一人称 | 误报修复已在本地补回归测试，尚未部署；部署后新建副本复验 |
| 20章真实 Provider 长跑 | 未开始 | 三章前置尚未通过，未启动20章 | 三章连续通过、两位盲评、清空历史证据 |
| 生产健康与迁移 | 可用 | 最终 `7c78ce5`；三个部署前 gzip 备份；`nc_starlume_authoring (head)`；healthz database/redis/worker 正常 | 持续监控 |

## 2026-08-21 扫榜书名策略修正（`9ffcd96` 已部署）

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 扫榜选题书名自动进入创作 | 可用 | `9ffcd96` 已部署；生产 run `d7147027-10aa-41c5-9ca7-e3caabfe6071` 已自动应用题名并进入 `generate_story_arc`；Progress 明确展示自动应用 | 新建扫榜作品的下一次生产回归 |
| 普通灵感/手动建书仍需人工选名 | 可用 | `bootstrap_novel` 继续传 `auto_confirm_title=False`；Progress 普通 human gate 未改变；本地前端回归覆盖 | 生产登录态回归 |
| 扫榜书名不能被 `plan_idea` 首个候选覆盖 | 可用 | `test_auto_confirm_run_keeps_ranking_title_in_run_context` 与 `29` 项后端目标回归；生产 `contents.title=context.selected_title` 一致 | 新建扫榜作品的真实工作流事件证据 |

> 历史需求 `NOV-W-002` 的“AI 策划后人工确认书名”仍适用于普通灵感流程；扫榜自动模式是明确例外，遵循既有 MVP 规则“扫榜自动模式可跳过普通确认”。

## 2026-08-20 朱雀报告驱动的生成期质量修复矩阵（`9e49213`）

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 生成时避免重复身体/任务/规则信息 | 已部署 | `scene_state_echo`；同一状态仅允许首次完整呈现，后续要求写变化、代价或行动 | 新正文与朱雀报告复验 |
| 生成时避免移动流水账 | 已部署 | `scene_procedural_motion`；移动段无事件、对白少且动作密集时生成期重写 | 新正文读者可读性与朱雀复验 |
| 生成时避免同一主语连续起笔 | 已部署 | `scene_subject_opening`；无对白移动段的主语段首比例门禁与专用重排 | 题材样本误伤回归与真实 Provider |
| 朱雀 95/5/0 | 未验收 | 报告实际为人工0%/疑似AI68.35%/AI31.65%；本批只部署生成规则，没有新外部报告 | 同一正文哈希的新代码样本与人工质量复核 |
| 三章/二十章真实 Provider | 未通过/未开始 | 本批不调用 Provider；既有三章第2章500停止，20章未启动 | 异常可观测性、单章稳定性和隔离实验 |

## 2026-08-20 单章真实 Provider 复验结果

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 生成期修复后真实单章调用 | 未通过 | 隔离副本启动 `active_chapters=0`；真实 `deepseek-chat` 第1章返回 `needs_review`，质量分87，阻断原因为 pacing | 生成期节奏/预算修复后重新单章 |
| 生成期自然性三类结构门禁 | 已接线 | 本章未命中 `scene_state_echo`、`scene_procedural_motion`、`scene_subject_opening` | 更多题材样本回归 |
| 七道发布门禁 | 未执行 | 生成状态未达到 `completed`，脚本 fail-closed 停止 | 先通过单章生成质量门禁 |
| 朱雀95/5/0 | 未验收 | 本批没有调用朱雀；内部自然性统计不能替代外部检测 | 同一正文哈希外部复验 |

## 2026-08-20 单章退化根因修复（`dd78b68`）

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 计划阶段不制造中段缓冲副线 | 已部署 | 长章节计划拒绝“中段放缓/舒缓/缓冲”独立节拍；辅助互动必须改变主线 | 修复后真实 Provider 单章 |
| 自然性统计正确识别对白 | 已部署 | `dialogue_count` 从原始正文计算，相关回归覆盖 | 真实题材样本回归 |
| 每章预算不被最终场景波动突破 | 已部署 | 取消最终场景超额放行，章节读者预算为唯一硬上限 | 修复后真实正文长度验证 |
| 朱雀95/5/0 | 未验收 | 本批仅修复生成期规则，未重新检测 | 同一正文哈希的外部复验 |

## 2026-08-20 编辑器辅助创作修复矩阵（`c851ed5`）

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 编辑器正文可读性 | 可用 | 作用域化编辑器画布/正文主题变量，浅色与深色规则分离；前端测试、lint、build 通过 | 登录态浏览器截图确认 |
| 当前章节人物/剧情/伏笔/世界观 | 可用 | 当前项目+章节精确查询，合并 V7 `character/plot/world`、`plot_threads`、`foreshadowings` 和已确认知识；排除 `content_id=NULL` 项目级污染 | 真实登录态页面确认来源与内容 |
| 人工主导 AI 辅助入口 | 可用 | 移除直接下一章/自动整章重写入口，统一为续写候选、选区润色、整章候选和 AI 共创助手 | 浏览器交互验收与候选人工应用 |
| 三章/二十章真实 Provider | 未通过/未开始 | 本批不调用 Provider；三章最新证据仍为第2章500停止，20章未启动 | 先补异常可观测性，再安排隔离实验 |

## 2026-08-20 本批实现覆盖矩阵

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 编辑器持久化 AI 会话 | 可用 | `authoring_sessions`/`authoring_messages`、`EditorAiChat` 刷新恢复和真实候选记录 | 生产 Provider 多轮与浏览器正式入口 |
| 当前章节剧情/人物/伏笔上下文 | 可用 | `POST /api/v1/authoring/context/{content_id}` 确定性聚合现有章节和 `knowledge_items` | 来源跳转、人工样本验收 |
| 最小故事 Bible 确认与影响扫描 | 可用 | 复用 `knowledge_items`，确认写版本快照并返回字面引用影响 | 生产登录态确认流程 |
| GPT/DeepSeek/豆包角色路由 | 已接线 | `authoring_{role_key}` 复用 `model_routes`；豆包走真实 Ark 适配器，缺密钥为 `needs_key` | 三模型真实 Key、同任务质量/成本 A/B |
| 真实码字事件账本 | 已接线 | `writing_events`、幂等客户端事件、人工/AI采用/撤销/保存事件 | 章节贡献汇总、导出和浏览器验收 |
| 平台人工发布回执 | 已接线 | `publication_human_receipts`；只有 accepted 才更新 published | 七道门禁、实际平台回执 |
| 三章真实 Provider 长跑 | 未通过 | 全新空副本 `b3cc7b91-74c6-4dd6-a122-aa2a003eea7a` 启动时0章/0个V7状态；真实 DeepSeek 第1章生成3320字、连续性通过、自动评审91，但开场门禁误判并进入 `needs_rewrite`，第2/3章未启动 | 部署开场门禁修复、全新空副本三章连续完成、两位盲评 |

### M7 最新阻断与生成前修复

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 短场景段首重复生成期修复 | 已接线 | `0d0f266` 后新空副本第1章通过，第2章第2场两次命中候选自身 `repeated_paragraph_opening`；本地 `2.31.0` 已增加专用段落重排指令，门禁阈值不变 | 全量回归、生产部署、真实复验 |
| 最新三章真实 Provider 验收实验 | 未通过 | `84e3957` 新空副本第1章 2655字/自动评审91/V7通过；第2章返回500，第3章未调用；trace `9f872ddf7b9045e3934c293758471402`，无章节2持久化诊断记录 | 先补失败可观测性，再决定是否安排一次隔离复验；不作为日常辅助创作工作流 |

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 早期爽点 floor 在生成时执行 | 已接线 | 生产 `78db748` 空副本 `adb68f72-6532-460f-856a-eea8fb07e71a` 第1章在正文后命中 `high < peak`；本地 `SceneDirector` 已将显式爽点契约校验和一次真实 Provider 修复前移到正文调用前，版本 `2.30.0` | 全量回归、生产部署、真实复验 |
| 生成前爽点修复不靠抬标签 | 已接线 | 修复 Prompt 要求可见结果/主动选择/反馈/余波在 beats 中落地；确定性复验仍要求契约字段、五阶段和最低强度 | Provider 修复样本与三章可读性 |
| 最新三章真实 Provider 长跑 | 未通过 | 第1章 3569字、连续性 `continuous`、自动评审90，因 `payoff_strength_insufficient` 停止；第2/3章未调用 | 先部署 `2.30.0`，再用全新空副本一次性复验 |
| 20章真实 Provider 长跑 | 未开始 | 三章前置未通过，未启动20章 | 三章真实通过、两位盲评和清空历史证据 |

### 三章尝试补充

三章验收脚本已在多个生产空副本执行过，但最新全新副本第1章仍因开场门禁误判进入 `needs_rewrite`，第2/3章未启动，故“三章真实 Provider 长跑”仍不能标记为已验收。当前修复已加入确定性开场分类和回归测试，待全量测试、部署后再用新空副本单次复验；20章保持未开始。

## 2026-08-20 M7 最新阻断矩阵

| 证据项 | 状态 | 结果 |
|---|---|---|
| 章节剩余额度显式进入生成 Prompt | 已部署 | `6489390`；第1章已真实生成通过，后续暴露自然性门禁问题 |
| 短类比密度阈值校准 | 已部署 | `d28a09f`；`count > max(6, chars/160)`，6处/1000字不再误阻断，密集链仍 fail-closed |
| 动作开场短环境引子校准 | 已接线 | 本地 `opening_variation.py`；首句未分类且前160字出现动作才放行；定向78 passed |
| 最新生产三章目标 | 未通过 | 副本 `72a2baf1-d5b7-4dd5-92d0-8efcf9f3f394` 第1章 2929字、连续性通过但因敏感词/开场词表缺口进入 `needs_rewrite`；第2/3章未启动 |
| 玄幻非露骨冲突策略 | 已接线 | 架空题材普通冲突词仅告警并保留命中，敏感类别和显式关闭配置仍阻断；新增策略测试 |
| 场景读者目标不作第二硬预算 | 已接线 | `scene_reader_budget_overrun` 改为 `scene_reader_budget_variance` 低优先级告警；章节总预算与后续场景最低表达空间保留硬门禁 |
| 最后完整场景自然波动 | 已接线 | `SCENE_SERIAL_GENERATION_VERSION=2.27.0`；无后续场景时自然波动额度 480 字；3545 字边界回归通过；第1章生产实测通过，第2章暴露跨场景段首误报 |
| 对白破折号生成期误报 | 已接线 | `SCENE_SERIAL_GENERATION_VERSION=2.28.0`；生成期只按去除完整对白后的叙述密度判断；定向/全量回归通过，待生产复验 |
| 类比密度重试路径 | 已接线 | `SCENE_SERIAL_GENERATION_VERSION=2.29.0`；自然性重试统一切换 `plain_factual`，不放宽非对白类比门禁；待生产复验 |
| 20章真实 Provider 长跑 | 未开始 | 三章前置尚未通过，不启动 |

> 本节是当前批次覆盖；后文历史矩阵保留作为需求演进记录，若冲突以本节和最新交接快照为准。

## 2026-08-20 辅助创作转型需求矩阵

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| Starlume AI 品牌与 GitHub 主仓库 | 已验收 | `3c376a0`；主仓库 ID `R_kgDOTT8enQ` 原地改名为 `Catatlina/starlume-ai-studio`，本地 origin 已更新 | 生产部署目录和兼容技术标识另批迁移 |
| 权威需求/架构/路线/门禁/交接文档 | 已验收 | `docs/Starlume-AI-开发文档/` 10份文档；链接、语法、真实性和交付门禁通过 | 后续实现必须持续同步 |
| 复用优先与最小开发契约 | 已验收 | `AGENTS.md`、新05规范、03路线和06实施方案；强制门禁已切换 | 每个功能切片需要逐项证明复用边界 |
| 人工主导默认创作主线 | 未开始 | 当前默认主链仍以既有生成流程组织；本批没有改变产品入口 | M1–M3正式入口、状态和浏览器验收 |
| AI候选预览后人工应用 | 可用 | 现有编辑器已有选区/整章候选、预览、应用、放弃和版本恢复 | 多轮会话、局部采用和陈旧修订冲突 |
| 编辑器真实多轮AI会话 | 已接线 | `EditorAiChat` 目前是前端本地消息外壳，调用现有单次改写；无后端多轮消息恢复 | M1真实Provider、持久化、刷新、失败和E2E |
| 当前剧情/人物/故事线上下文 | 已接线 | V7 ChapterContext、状态、人物、时间线和伏笔能力存在；编辑器尚无统一来源面板 | M2聚合接口、来源跳转、人工确认 |
| 统一创作圣经 | 已接线 | 现有规划、知识、小说元数据和V7状态可复用；缺统一版本与人工确认入口 | M3最小圣经闭环和影响分析 |
| GPT/DeepSeek/豆包角色编排 | 未开始 | Gateway支持多Provider方向，但没有按创作角色配置和真实三模型证据 | M4角色路由、真实Key、成本和A/B证据 |
| 真实码字与AI贡献账本 | 未开始 | 现有版本/`ai_calls`可复用，但没有 writing session/event 和贡献重算 | M5离线/幂等/撤销/隐私验收 |
| 平台定稿与官方码字衔接 | 已接线 | 现有发布准备、variant、披露和门禁可复用；没有创作摘要和官方回执适配 | M6番茄定稿包、人工回执和规则复验 |
| 新辅助创作主线三章验证 | 未开始 | 本批未调用Provider、未创建隔离副本 | M1–M6单章通过后再跑空历史三章 |
| 外部95/5/0报告 | 未开始 | 内部门禁不等同第三方检测器；本批无真实报告 | 正文哈希一致的真实报告与人工质量证据 |

> 本矩阵明确区分“方案与治理已验收”和“产品功能未开始/已接线”。提交文档不等于新的辅助创作主线已完成。

## 2026-08-20 场景预算修正与最新三章真实 Provider 矩阵

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 场景预算只作软节拍提示 | 已部署 | `7e55912`；715字不再作为独立硬阻断，章节总预算仍 fail-closed | 真实单章稳定生成 |
| 生成期比喻修复路径 | 已部署 | `89d8698`、`0dcf665`；纯比喻和比喻+预算组合失败均走字面现场重写 | Provider 仍可能重复产出过密比喻 |
| 比喻密度误阻断校准 | 已部署 | `47d32ac`、`7e55912`；定向回归 `110 passed`，按场景长度计算阈值 | 真实多章可读性 |
| 最新三章真实 Provider 验证 | 未通过 | 空副本 `11203278-a624-4c7c-9278-d3a304806be0`，目标3章；第1章第2场8处/1237字，`生成成功=0/3` | 先通过单章，再重跑三章 |
| 清空历史保护 | 已验收 | 最新副本失败后0章/0个V7状态；源作品保持10章 | 持续保留副本隔离 |
| 朱雀95/5/0 | 未验收 | 仍无本轮真实朱雀报告 | 真实朱雀检测与回归闭环 |

## 2026-08-20 三章真实 Provider 验证补充矩阵

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 简化链真实 Provider 单章 | 未通过 | `45dbea7`，空副本 `95870368-4bce-4c5c-856c-61c820aeb85f` 第1章失败：3处比喻链，750>715 | 事件短单元生成 |
| 三章真实 Provider 验证 | 未通过 | 目标3章，结果 `生成成功=0/3`；第1章失败即停止 | 新单元链先通过单章，再重跑3章 |
| 清空历史保护 | 已验收 | 最终副本创建时0章，失败后0章/0个V7状态；原作保持10章 | 持续保留副本隔离 |
| 朱雀95/5/0 | 未验收 | 本次仍只有内部生成门禁，没有朱雀真实报告 | 真实朱雀校准闭环 |

> 本轮三章没有“部分完成”章节；失败正文未持久化，不能把 `0/3` 解释成通过。

## 2026-08-20 生成主链简化补充矩阵

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| V7 canonical 生成链去冗余 | 已部署 | `6afaea6`：每场最多两次 Provider 调用，移除自动结构化候选和局部 Best-of-3 嵌套 | 单章真实 Provider 复验 |
| 失败语义保持 fail-closed | 已部署 | 第二次完整重写仍失败时直接阻断，不返回伪成功正文；`110 passed` | 生产失败路径复验 |
| 朱雀95/5/0 | 未验收 | 本次是流程简化，不含朱雀真实评分反馈 | 朱雀校准样本与外部检测闭环 |
| 20章真实 Provider 长跑 | 未开始 | 本次未启动长跑 | 单章稳定通过后再启动 |

> 旧的 `chapter_loop/deai_pipeline` 仍保留以兼容历史 API，但不是 V7 canonical 生产正文主路径；后续如确认无调用者，再单独清理。

## 2026-08-18 生成协议复盘与最新真实 Provider 证据

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 豆包经验融合到生成期 | 已部署 | `78fb69d`：正向自然叙事协议、四类结构化生成路径、失败候选隔离、低温度重试、结构化纯叙事 Provider 候选 | 真实多章稳定性 |
| 后置审计仅作兜底 | 已部署 | 生成链路在场景生成阶段检查自然性；结构化候选仍由真实 Provider 生成；未启用整章后置重写作为主修复 | 真实盲评与连续性 |
| 新代码真实 Provider 单章 | 未通过 | 清空历史副本最新结果 `生成成功=0/1`；第1场7处比喻表达，约890字超过715字预算，V7 阻断且未落库 | 先稳定通过单章 |
| 20章清空历史长跑 | 未开始 | 未启动；当前不满足单章稳定门槛 | 20章逐章正文、门禁、连续性和可读性报告 |
| 朱雀95/5/0 | 未验收 | 内部生成期风险门禁不等同朱雀结果；尚无本轮新正文的朱雀报告 | 真实朱雀检测与回归样本 |
| Provider A/B | 未开始 | 生产仅配置 `deepseek-chat`，无 GPT/Luna key/真实路由 | 同一协议、同一场景、真实成本与质量比较 |

> 本矩阵以最新 `78fb69d` 真实证据为准；旧版本单章通过记录仍保留作为历史证据，不代表本轮修复后已稳定。

## 2026-08-18 生产真实单章与生成期节奏修复补充矩阵

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 本轮代码提交、推送、生产部署 | 已验收 | `39ff597` 已推送并部署；部署前备份 gzip 校验通过；公网 healthz、Alembic、容器健康正常 | 持续运行观察 |
| 生成期首场节奏控制 | 已部署 | 首场 400–420 字；前120字变化、前420字可见后果/选择/新风险；相关目标测试纳入 93 passed | 多章真实 Provider 回归 |
| 新代码生产真实 Provider 单章 | 已验收 | 空历史副本 `b9ca26eb-e891-4156-984a-56bb76c2481f` 第1章 `f00e1934-d711-407c-b9b6-6cab8fb04859`；3277字、V7 completed、review 90.0、开场通过、联网 live/miss | 多章/20章稳定性 |
| 单章七道发布门禁 | 已接线 | 实际执行 7/7，5项通过；content_quality=90、payoff_density=75 | 平台策略 stale、披露人工确认、正式发布 |
| 20章清空历史长跑 | 未开始 | 本次只完成受控单章；验收副本创建时 active_chapters=0、v7_states=0，未续跑第2章 | 真实20章逐章生成、门禁和连续性报告 |
| 朱雀 95/5/0 | 未验收 | 当前仅有生成期特征卡、局部候选和内部风险门禁 | 朱雀真实接口/报告与样本校准 |

> 本矩阵明确区分“单章真实生成通过”“发布门禁通过”和“20章长跑完成”；本轮没有把前者升级成后两者。

## 2026-08-17 生成优先与清空历史长跑补充矩阵（当前批次）

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 正/负样本进入生成期特征卡 | 已接线 | `prose_generation.py` 提炼统计质地和规则，样本 SHA-256/检测器/Provider 元数据保留，Writer Prompt 不含原文 | 多题材、多检测器样本校准 |
| 生成期 Critic 与低风险冻结 | 已接线 | 场景候选生成后返回段落风险索引和锁定集合，最多四个高风险段进入局部处理 | 真实 Provider 单章运行证据 |
| 局部 Best-of-3 与禁止恶化 | 已接线 | 三个独立组织方式候选；段落边界、篇幅、重复段、内部风险上升均拒绝 | 外部朱雀/API评分接入；多章回归 |
| 朱雀 95/5/0 | 未验收 | 当前只有内部生成期质量选择，不冒充外部检测分数 | 真实朱雀报告/官方接口和样本校准 |

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 生成期自然、可读、连续，后置审计只兜底 | 已部署 | `GenerationEngine` 场景串行交接、首场前220字事件约束、场景长度硬约束/一次重试；`StoryDirector` 与 canonical runtime 默认 `allow_rework=False`；生成链路定向48 passed | 真实 Provider 20章质量结果、人工盲评 |
| 所有长跑先清空历史章节 | 已接线 | 新副本准备脚本只复制设定；长跑脚本硬拒绝 active contents 或 V7 story states 非空；生产新副本 `9070f234-9de0-4131-a301-bf3ddddeb1e9` 创建时为0章 | 20章实际完成 |
| DeepSeek 真实生成复跑 | 进行中 | 新空副本已启动20章真实 Provider 长跑；旧副本首章因 pacing=87.3 停止 | 必须等待报告，不得提前宣称 |
| GPT-5.6 Luna 对照 | 已接线 | UnifiedGateway 支持 OpenAI transport，`gpt-5.6-luna` route 可配置 | 生产 OpenAI key、同 prompt 真实 A/B |
| 95/5/0 外部门禁 | 未验收 | 当前只有内部生成期自然性约束和审阅证据 | 朱雀/其他外部检测真实报告、披露人工确认 |
| 七道门禁、披露确认、正式发布 | 未完成 | API/页面/状态机已部署 | 有效平台政策、人工确认、正式发布操作证据 |

> 本矩阵区分“已接线/已部署/进行中”和“已验收”；代码测试通过不替代真实 Provider、外部检测和人工确认。

## 2026-08-17 最新代码质量复核、生产样本与登录态验收（当前批次）

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| statistics_v1 确定性章节统计 | 可用 | 最新分支专项通过；统计结果已补齐局部修复所需的句子正文、段落索引和字节范围；生产代码为 `bb04e5a` | 生产真实章节快照复核 |
| 七道发布门禁引擎 | 已接线 | v0.9.2自测 `60 passed`；生产已有6章真实正文门禁；第6章真实 Provider 语义样本返回 `85.0/1/ending_pressure=true` | 生产真实 Provider 20章长跑 |
| 发布状态机与AI披露阻断 | 可用 | `publish_ready`要求全部阻断门禁证据；披露生成不再自动确认；不存在的披露确认明确失败；生产迁移已到 `nc_v11_disclosure_payoff (head)`；真实披露样本保持 `draft` | 登录态运行七道门禁并完成确认流程 |
| 发布准备 API 项目/作品/章节范围 | 已接线 | 章节、变体、平台配置、披露均经项目成员与关联作品校验；API 路由实际返回 `401 authentication required` | 生产双用户接口回归 |
| 20章真实Provider长跑验收 | 未开始 | 长跑脚本已改为真实 `contents.seq`、真实 V7 `v6_content_id`、数据库平台配置和证据写入；生产只读确认现有第6章 `needs_rewrite` | 先修复前置章节或用户授权创建正确配置新书 |
| 生产部署 | 已验收 | 生产代码 `bb04e5a`；迁移 `nc_v11_disclosure_payoff (head)`；API/Worker/Beat/数据库/Redis/Frontend 容器运行；部署前备份 gzip 校验通过 | 七道门禁与发布确认全流程仍待登录态实测 |
| 前端发布准备页面 | 已验收 | `PublishingPreparation` 已接入主导航、真实平台/变体/门禁/披露 API；前端 `57 passed`、TypeScript、生产构建通过；新域名登录态 `#/publish` 流程和浅/深主题均复验 | 运行七道门禁并完成发布确认流程 |
| AI披露文案生成 | 已验收 | `publishing.ai_disclosure` 真实 Provider 结构化输出；生产样本为 `generated/provider`、模型 `deepseek-chat`，变体仍为 `draft` 未确认 | 多章节/20章样本与最终平台文案复核 |
| payoff_density 真实检测 | 已验收 | `publishing.payoff_semantic` 生产真实样本返回 `semantic_score=85.0`、`payoff_count=1`、`ending_pressure=true`，低置信度和 malformed output fail-closed | 多章节/20章语义质量验收；当前实现仍不是完整人工爽感评估 |

### 本批质量证据与环境边界

- `backend/.venv/bin/python -m pytest -q backend/tests/test_publishing_api_scope.py backend/tests/test_publishing_service_guards.py backend/tests/test_statistics_v1.py`：`19 passed`。
- `backend/.venv/bin/python backend/tests/test_publishing_v092.py`：`60 passed`。
- `python3 scripts/verify_ai_truthfulness.py`、`python3 scripts/verify_delivery_claims.py`、`bash scripts/ai_development_gate.sh`：均通过。
- 当前全量基线 `PYTHONPATH=backend backend/.venv/bin/pytest -q backend/tests`：`1121 passed, 138 skipped, 1 xpassed, 2 warnings`，退出码 0；此前失败基线来自本机 PostgreSQL/Redis 未启动，已不再作为当前结论。
- 当前分支干净且已推送至 `origin/agent/publishing-v0.9.2`；旧工作区改动保存在 `stash@{0}`。生产单样本已调用真实 Provider，但20章长跑未调用、未写生产正文；公网 healthz 本次观察为 Cloudflare 到 `/login` 的 `302`，不作为公网健康验收证据。

## 2026-08-11 v0.9.2 出版准备层（历史开发快照）

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| statistics_v1 确定性章节统计 | 代码级可用 | `app/v7/quality/statistics_v1.py`；UTF-8字节偏移/四级切分/双哈希/异常标点；11单元测试通过；同一输入字节级一致 | 生产迁移后真实章节统计快照验证 |
| 七道发布门禁引擎 | 代码级可用 | `app/v7/quality/publishing_gates.py`；content_quality/continuity/payoff_density/readability/platform_compliance(三子门禁)/ai_disclosure/external_risk；18单元测试通过 | 真实Provider生成章节跑门禁、publish_ready通过率统计 |
| 发布状态机（draft→quality_candidate→publish_ready→published） | 代码级可用 | `app/v7/services/publishing_service.py`；合法转换校验；旧reviewed保持兼容；12状态机测试通过 | 生产状态转换实测、前端状态展示 |
| 多平台发布变体（B方案） | 代码级可用 | publication_variants表；platform_profile_revision/metadata_revision/content_revision/publication_status；正文可共用冲突时专属修订 | 真实多平台变体创建、平台专属正文修订验证 |
| AI披露五态政策阻断 | 代码级可用 | ai_disclosure_records表；allowed/allowed_with_human_editing/required_disclosure/unknown/prohibited；披露文案生成放v1.1 | 真实平台政策配置、披露确认流程验证 |
| ChapterContext五类上下文融合 | 代码级可用 | `app/v7/services/chapter_context.py`；GenrePack+StyleCard+CharacterVoiceCard+StoryState双快照+CausalContract+Platform；超预算停止不静默切掉；12单元测试通过 | 接入生成引擎、真实上下文token预算验证 |
| 局部修复引擎（替换整章去AI重写） | 代码级可用 | `app/v7/quality/local_repair.py`；风险句定位→1-3处局部修复→复审，最多3轮；AI修复函数可注入；5单元测试通过 | 接入真实Provider AI修复、修复后质量复测 |
| 发布准备API（18端点） | 代码级可用 | `app/api/v1/publishing.py`；已在main.py注册；统计/门禁/平台配置/变体/披露/人工编辑/局部修复/就绪检查 | 生产API冒烟测试、前端对接 |
| 平台规则policy_status门禁 | 代码级可用 | platform_publication_profiles表；policy_status=confirmed|stale|unknown；stale/unknown不能publish_ready；内置番茄/起点/晋江示例(stale) | 用户手动确认平台规则流程验证 |
| external_flagged发布页展示 | 代码级可用 | external_risk门禁默认非阻断（仅prohibited时阻断）；命中时写warning要求展示；publish-readiness端点返回external_ai_flagged | 前端发布确认页展示验证 |
| 前端发布准备页面 | 未开始 | API已就绪，前端组件未开发 | 前端开发、用户操作流程验收 |
| 20章真实Provider长跑验收 | 未开始 | 代码和测试通过，未真实生成 | 创建新书、20章生成、每章七门禁、验收报告 |
| 生产部署 | 未开始 | 分支agent/publishing-v0.9.2未提交推送 | Git提交→推送→SSH部署→alembic迁移→容器重启→prod_smoke |

本节为v0.9.2开发中状态；代码存在+单元测试通过≠生产可用，必须完成部署和真实长跑后才能升级状态。

## 2026-08-17 联网创作真实证据

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 作品级联网创作开关 | 已验收 | 原作 `4ee9db30-98c7-40d5-9484-12432efed69e` 通过正式生成设置 API 切换为 `required`；生产 Tavily Key 已配置 | 真实正文质量仍需验收 |
| 真实联网搜索与灵感卡 | 已验收 | 诊断作品 `44c557a8-8e4d-461a-8079-eace38d260cf` 实测 `live/miss`，2 查询、10 来源、5 卡片，真实 DeepSeek 整理调用、成本和 `web_research.completed` 已持久化 | 不能替代一章正文和20章长跑 |
| 联网正文生成（单章） | 已验收 | 正确 V7 入口真实 Provider 第1章 HTTP 200；V7 completed，3072字正文持久化，`web_research=live/miss`、6卡片、9来源、Prompt execution/成本/审阅记录齐全 | 20章长跑、长期连续性与外部95/5/0仍未验收 |

## 2026-08-10 最新生产证据

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 生成质量旁路 fail-closed、真实品类包进入 V7 | 可用 | `main@70688f9` 已部署；生产迁移到 `nc_v7_genre_packs` head；backend/frontend/frontend-test/security/E2E 全通过；生产 smoke 15/15、浏览器 v2 1 passed | 真实 Provider 章节样本、两本书各 20 章长跑、人工爽感/连续性复核 |
| 生产服务与数据库迁移 | 已验收 | 公网 healthz 200；database/Redis/Worker 正常；迁移前备份 gzip 校验通过；容器重建后全部 running | 持续运行观察 |
| 真实网感研究配置 | 已接线 | healthz 显示 Tavily provider 已配置；代码仍要求真实搜索和真实 AI 整理，失败不降级为 mock | 真实联网一章和长跑验证 |

本节是当前部署证据；下方较早批次中的“待发布/未部署”文字保留为历史记录，不覆盖本节结论。

## 2026-08-10 生成质量旁路收紧（已部署，历史批次记录）

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| Provider/质量失败不得伪装通过 | 可用 | V7 DeAI、编辑器包装和生成前置门禁均 fail-closed；质量专项回归通过 | 生产真实 Provider 失败注入复测 |
| 场景计划不能以空 beat/缺阶段进入正文 | 可用 | `SceneDirector.validate_scene_plan_contract`；4–6 beat、字数预算、五阶段校验；专项回归通过 | 生产真实 Provider 计划样本 |
| 实时审阅必须验证跨章正文连续性 | 可用 | `review_service._continuity_evidence` 加入 transition contract + prose continuity；模型高分不能替代第 2 章起的确定性证据 | 生产登录态章节实测 |
| 正文镜像/平行版本不得进入完成状态 | 可用 | `CHAPTER_MIRROR_HARD_GATE=True`；标题基名+开头锚点检测命中即质量失败 | 生产两本书长跑和人工复核 |
| 品类上下文加载失败不得静默降级 | 可用 | 已配置 `genre_id` 时空品类包直接停止生成；不再使用空上下文冒充成功 | 生产真实品类包生成 |

## 2026-08-10 爽文网感研究链（已接线，真实质量未验收）

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 生成目标以爽、快节奏、读者兴奋为主 | 可用 | 新书默认爽感快节奏风格；V7 既有五阶段爽点契约继续作为结构硬门禁；研究卡注入场景规划和正文 Writer | 真实 Provider 章节样本与人工爽感盲评 |
| 生成前联网提炼网感灵感 | 已接线 | `WebResearchService` → Tavily `/search` → 真实 AI 原创灵感卡 → V7 Context/Scene/Writer；无 Key/搜索失败直接失败 | 服务器配置 Tavily Key、真实一章验收 |
| 研究结果缓存、审计和来源可追溯 | 可用 | 复用 `v7_event_logs`；查询哈希、TTL、卡片、来源域名、成功/失败事件；不存完整网页正文 | 生产数据库迁移/事件回放验证 |
| 现有作品可切换研究策略 | 可用 | `GET/PUT /api/v1/novels/{novel_id}/generation-settings`；版本快照和 `audit_logs`；Settings 页面有读取/保存测试 | 生产登录态浏览器点击验证 |
| Docker/环境变量可配置 | 已接线 | compose、`.env.example`、`.env.production.example`、healthz 状态字段已补齐 | 生产注入 Key 后重建并检查容器环境 |
| 真实联网生成质量验收 | 未开始 | 本地仅完成失败/成功协议 mock 的单元契约；远端无 Tavily Key | 一章实跑、两本各 20 章长跑、上下章连续性和人工评审 |

## 2026-08-06 Demo 视觉骨架落地（未部署）

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 页面不再沿用旧版骨架 | 本地可用 | Layout 页头、首页 KPI/作品表格、Wizard 步骤轨道、书库/进度/扫榜/设置统一面板已落地 | 发布后生产截图复核 |
| 首页呈现 Demo 式工作台信息密度 | 本地可用 | 四项 KPI 使用真实书库/运行数据；无数据显示“未评分/—”，不伪造数字 | 多作品、多章节真实数据复核 |
| 主导航保持不变 | 已验收（代码级） | `Layout.tsx` 保留原 `NAV_ITEMS` 顺序和入口，仅增加页头品牌/面包屑 | 生产发布后点击回归 |
| 视觉改造不破坏真实功能 | 已验收（本地） | lint、build、49/49 前端测试、页面烟雾 1/1；浏览器核对六个主入口 | push/deploy 后生产走查 |

本批不能标记为生产已部署；发布和线上验收仍是独立步骤。

## 2026-08-06 AI 味词库与编辑会话（已推送部署）

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| AI 味词库前台可查看、配置、编辑 | 已验收 | `Settings.tsx`“质量规则”入口；11 类词库可开关、改名、改说明、增删词条；`Settings.quality.test.tsx`；生产登录态页面显示 11 类/171 个启用信号 | — |
| AI 味词库可持久化并回读 | 已验收 | `/api/v1/quality/ai-flavor-lexicon` GET/PUT/RESET；现有 `settings` 表；commit `e3a56b2`；生产管理员 GET/PUT/RESET 3/3 | — |
| 词库参与生成前约束但不变成单词禁令 | 可用 | `render_ai_flavor_guidance` 注入生成、续写、人文化 Prompt；`mode=advisory`、`hard_gate=false`；V7 审阅保留原文证据和题材豁免 | 新 Prompt Provider 长跑与人工误伤评估 |
| 编辑器支持自由输入 AI 修改意见 | 可用 | `EditorAiChat.tsx` 与 `EditorAiChat.test.tsx`；先生成预览，确认后才应用 | 真实编辑器 Provider 正向操作复核 |
| 本批质量/真实性门禁 | 已验收 | 后端 `1035 passed`、前端 `49 passed`、TypeScript/lint/build、`verify_ai_truthfulness.py`、`ai_development_gate.sh` clean | 真实生成质量仍需独立外部验收 |

本批“可用”仅表示代码和离线回归闭环，不表示生产质量或人工盲评已验收；真实 Provider 20 章、两位评审和最终读者体验仍按独立门禁处理。

## 2026-08-05 部署后真实证据补充

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 生产部署与服务健康 | 已验收 | `d1253f2` 已部署；前端/healthz/登录/项目列表通过；容器健康 | 持续运行观察 |
| 测试工作空间不再污染主账号列表 | 已验收 | 书库查询过滤 `is_deleted=FALSE`；主账号仅显示主工作室 | — |
| 主工作室历史章节归属 | 已验收 | `canonical=73`、`unresolved=0`；生产 dry-run `scanned=0` | 新导入项目按流程复核 |
| 新爽点契约在生成前生效 | 可用 | `chapter-payoff-contract-v2`、四档强度、五阶段节拍、可见反馈和去 AI 味保护已部署 | 新 Prompt 20 章真实长跑、两位人工盲评 |

## 2026-08-05 历史章节归属治理

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 历史无归属章节保留且不污染主书库 | 可用 | `nc_legacy_chapter_scope` 为章节补充范围状态；本地测试产物已按外键依赖清理；最终仅保留主账号/主工作空间，主账号正文数据前后均为 0 | 生产迁移后再次核对 |
| 历史章节扫描、证据和决议可追踪 | 可用 | `chapter_scope` 服务和 `/api/v1/chapter-scope/*` 提供 dry-run、候选证据、状态、操作者和审计日志 | 生产环境执行扫描 |
| 高置信历史章节安全自动归属 | 可用 | 只有直接 Novel/Run/Batch provenance 或强标题证据且超过置信度与边际阈值才会自动写入 `legacy_resolved` | 生产样本验证自动绑定准确率 |
| 模糊历史章节人工确认 | 已接线 | `pending` 决议列表和 `POST /chapters/{chapter_id}/bind` 已接入；绑定前不会进入 V7 写入链 | 需在真实生产数据扫描后确认，不对本地测试产物逐条处理 |
| 正常新章节和 V7 编辑/审阅/生成必须有合法 Novel scope | 可用 | API、worker、V7 runtime、修复和场景入口统一执行 fail-closed scope gate | 生产部署后 smoke |

“已接线”表示操作入口和状态闭环已经存在，但尚未完成真实人工处理；不能把未确认历史归属写成已验收。

## 2026-08-04 未完成项目最新证据

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| AI 真实性/开发门禁 | 已验收 | `verify_ai_truthfulness.py` 通过；`bash scripts/ai_development_gate.sh` exit 0；allowlist 仅覆盖不生成正文的确定性扫描与草稿持久化 | 后续新增生成函数仍需保持真实 Gateway 证明 |
| 生产 V7 单链路 20 章长跑 | 可用（真实自动证据） | 20/20 `reviewed`、20/20 `v7_quality_gate_passed`；平均 88.66、最低 87.0；连续性 clean 20/20；重复段落比例 0；第三人称失败 0；transition contract 20/20；证据在 `artifacts/v7-20-chapter-20260804/` | 两位独立人工盲评 |
| V7→V6 书库/编辑器/导出产品链 | 可用（真实生产链） | 20 章写回 V6 `contents`；编辑器、完成度、TXT/Markdown/EPUB 均返回真实证据；`ready_for_release=false` 正确保留人工门禁 | 人工应用/阅读复核与最终发布判定 |
| 人工盲评包 | 已接线 | `blind-review-packet.md` 20 个匿名 case；`blind-scores.template.csv` 评分模板 | 当前 0/20 case 达到两位评审覆盖 |

“可用（真实自动证据）”不等于“生成质量已最终验收”；人工评审仍是故意保留的独立门禁。

## 2026-08-04 页面可用性整改批次（代码级）

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 创作历史默认折叠 | 可用 | `Progress.tsx` 使用原生 `details/summary`；目标回归覆盖展开、打开记录、加载更多 | 生产账号视觉复核 |
| 审阅综合评分与审计准确性展示 | 可用 | `Review.tsx` 读取 V7 `overall_score`、`dimension_scores`、`audit_report`、连续性结果；测试覆盖真实分数、折算分、33 维来源标注 | 真实 Provider 审计覆盖与人工相关性 |
| V7 质量与运行监控页面 | 可用 | `V7Dashboard.tsx` 既有真实 API 数据流；`styles.css` 补齐总览/运行/账本/Prompt/错误/权限/移动端样式；Playwright V7 走查 2 passed | 生产视觉截图与真实运行数据持续观察 |
| 创作向导写作风格 | 可用 | `Wizard.tsx` 预设下拉 + 自定义高级入口；生成请求仍提交 `style` 字段；Wizard 测试覆盖选择和自定义 | 真实生成样本验证预设对正文质量的提升 |

上述状态是页面和代码级状态，不等于真实 Provider 长跑、人工盲评或最终生成质量验收。

## 2026-08-02 本轮一次性质量整改（当前发布批次）

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 生成前质量控制与跨章交接 | 可用 | V7 GenerationEngine、ContextAssembler、TruthStore、transition contract、场景目标/阻碍/选择/代价约束 | 真实 Provider 20 章长跑 |
| 33 项内部审计与读者体验门 | 可用 | `audit_dimensions.py`、`ReviewEngine`、V7 quality gate；关键维度不能被平均分掩盖 | Provider 完整审计样本与人工相关性 |
| 去 AI 味规则与生成前约束 | 可用 | `DEAI_IRON_RULES`、`deai_metrics.py`；按密度/重复/同构风险，不禁单个标点 | 真人样本误伤率和盲评 |
| 去 AI 味/人文化内容保真 | 可用 | `text_quality.py`、Bootstrap/V7/编辑器统一字符比例与段落保真门 | 真实 Provider 多轮重写 |
| 失败状态、重试和编辑器兜底 | 可用 | App/Progress/WorkspaceDashboard/Editor；节点失败不再显示已完成，应用前预览且保存失败不清理预览 | 生产真实失败注入复测 |
| 本轮自动回归与真实性门禁 | 可用 | 后端 878 passed、138 skipped、1 xpassed；前端 34 passed；build；truthfulness 通过 | 生产部署后 smoke |

运行时代码 `2fba0be` 已部署并通过生产 smoke 与 V7 浏览器走查；真实 Provider 长跑、人工盲评和最终读者体验仍按外部证据单独验收。

## 2026-08-02 融合开发批次（工作树，基线 `94fc731`）

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 全站/按类型扫榜及范围证据 | 可用 | `backend/app/api/v1/ranking.py`、`RankingCenter.tsx`；离线榜单契约回归通过 | 真实平台刷新和平台字段覆盖 |
| 选题候选市场字段与快照来源 | 可用 | `topic_candidates.meta` 复用现有迁移；市场分析回归通过 | 真实榜单样本和人工选题评审 |
| 33 维内部审计 + 7 宏观分数 | 可用 | `backend/app/v7/quality/audit_dimensions.py`、`ReviewEngine`；V7 质量契约回归通过 | Provider 必须真实返回完整 33 项并做长篇质量复核 |
| 跨章状态交接与七域真相投影 | 可用 | `continuity.py`、`truth_store.py`、V7 Director；transition contract 回归通过 | 真实多章运行和人工连贯性评估 |
| 去 AI 味确定性指标与规则学习灰度/回滚 | 已接线 | `deai_metrics.py`、`rule_learning.py`；规则状态机回归通过 | 真实章节样本验证误伤率和读者体验 |

本批工作树尚未提交或部署；“可用/已接线”只代表代码级闭环和离线回归，不代表生产质量验收。

## 2026-08-02 最新单链路决策

质量对比已完成选型：V7 真实 20 章平均 92.0、最低 91.0，V6 平均 79.6、最低 72.0。故正文生成不再维护双轨，V7 为唯一 canonical chain；V6 只承担兼容事实、`contents`、编辑器和导出。

## 2026-08-02 状态真实性修复与生产刷新

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 失败/等待结果不得显示已完成 | 可用 | `7851b7f`；canonical `pending_approval / needs_review / failed` 按真实状态投影到 Bootstrap 节点；历史截图 run 已定向纠正；canonical 回归 10 passed | 更多真实 Provider 运行覆盖 |
| 质量拒绝草稿可见且不可发布 | 可用 | V7 rejected draft 写回 V6 `contents.status=needs_rewrite`，保留质量分、问题和版本快照；代码回归通过 | 生产真实质量长跑与人工盲评 |
| 生产刷新与入口可达 | 可用 | `7851b7f` 已部署；healthz 200；`prod_smoke.py` 15/15 | 真实生产 20 章质量验收 |

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 正文生成唯一链路 | 可用 | `7851b7f` 已部署；`continue`、批量、自动续写、人工重生成和 Bootstrap 首章均委托 V7 Director；生产 smoke 15/15，V7 质量通过后幂等写回 V6 `contents` | 生产 20 章 Provider 长跑 |
| V6 兼容承载 | 可用 | V6 仅作为事实/知识/章节存储、编辑器和导出层；V7 结果保留 `canonical_engine=v7`、run 和 transition provenance | 目标部署环境真实回放 |
| 生成质量目标 | 可用 | V7 真实双轨自动证据优于 V6；本轮代码回归已通过 | 生产 20 章、两位人工盲评；不能标记已验收 |

> 状态只使用：未开始 / 已接线 / 可用 / 已验收。  
> “可用”表示确定性真库链路已通过；“已验收”还要求对应真实 Provider 或生产证据。

| 编号 | 需求 | 前端 | 后端/数据 | 状态 | 当前证据 | 升级门禁 |
|---|---|---|---|---|---|---|
| NOV-G-001 | 用户可注册、登录、退出 | `LoginPage.tsx`、`App.tsx` | `/auth/register`、`/auth/login`、JWT | 已验收 | E2E 主线① + 生产 `novel.xyjin.xyz` 认证 smoke | — |
| NOV-G-002 | 展示八个小说入口（含扫榜选书） | `Layout.tsx`、`App.tsx` | 不改历史数据 | 已验收 | E2E 主线① + visual.spec 截图 + 生产巡检 | — |
| NOV-G-003 | 旧入口迁移、未知入口 404 | `App.tsx`、`NotFoundPage.tsx` | 无 | 已验收 | E2E 主线② + 生产路由 smoke | — |
| NOV-G-004 | 浅深色、主题记忆、响应式 | `ThemeProvider.tsx`、`styles.css` | 浏览器偏好 | 已验收 | 单测 + visual.spec 桌面/手机截图 + 生产巡检 | — |
| NOV-G-005 | 公共页多书切换且作品、run、章节、编辑器、审阅一致 | `Layout.tsx`、`App.tsx` | contents、latest run；账号隔离缓存 | 可用 | E2E 主线⑤：两本真书 + 延迟旧请求，快速切换后编辑器/审阅保持最后选择；选择器仅三个公共页；确定性 E2E 5 passed / 4 skipped | 同提交 CI；生产切书 smoke |
| NOV-H-001 | 首页显示真实书籍和运行状态 | `WorkspaceDashboard.tsx` | `/library/books`、`/runs/latest` | 可用 | 单测、E2E 空状态 | 有书/有运行生产证据 |
| NOV-W-001 | 输入创意、题材、风格、篇幅并启动 | `Wizard.tsx` | Bootstrap API、真实 Gateway | 已验收 | protected E2E 1 passed (2026-07-28, run 8f1fd62b, 19 ai_calls)；修复 step 校验 bug；生产 healthz /wizard smoke | — |
| NOV-W-002 | AI 策划后必须人工确认书名 | `Progress.tsx` | `human_confirm_title` 节点 | 已验收 | protected E2E 1 passed (2026-07-28, run 8f1fd62b, 19 ai_calls)；waiting_human 真实出现并点选定名；生产 smoke | — |
| NOV-P-001 | 展示真实节点、产物、失败和重试 | `Progress.tsx` | Runs、nodes、retry API | 已验收 | 单测(Progress.test.tsx：空态、人工定名、失败原因+重试打到 `/runs/{id}/nodes/{key}/retry`)；protected E2E 小说主线③ 运行中真实节点截图(protected-01)；小说主线⑤ 人工定名(protected-02)/19 节点完成(protected-03, 19 ai_calls)；生产 smoke | — |
| NOV-L-001 | 书库加载、搜索、筛选、排序 | `BookLibrary.tsx` | `/library/books` | 可用 | 真实空态与建书 E2E | 分页/筛选 E2E |
| NOV-L-002 | 详情、章节目录和导入 | `BookLibrary.tsx` | novel/detail/import API | 可用 | E2E 主线③ | 生产 smoke |
| NOV-L-003 | TXT/MD 导出 | `BookLibrary.tsx` | export API | 已验收 | protected E2E 1 passed (2026-07-28, run 8f1fd62b, 19 ai_calls)；导出正文断言通过；生产 smoke | — |
| NOV-E-001 | 编辑章节并真实持久化 | `Editor.tsx`、`RichEditor.tsx` | `PUT /contents/{id}`、versions | 已验收 | E2E 主线③刷新持久化；生产 smoke | — |
| NOV-E-002 | AI 编辑先预览再应用或放弃 | `Editor.tsx`、`editorPreview.ts` | content AI API、版本保存 | 已验收 | 3 条单测；protected E2E 1 passed (2026-07-28, run9, 2 ai_calls)：续写→预览→放弃后原文不变→应用→版本恢复；生产 smoke | — |
| NOV-E-003 | AI 失败不覆盖原文 | `App.tsx`、`Editor.tsx` | Gateway 显式失败 | 已接线 | 代码与单测边界 | 浏览器失败注入 E2E |
| NOV-E-004 | 版本查看与恢复 | 编辑器版本区 | versions API | 已验收 | protected E2E 1 passed (2026-07-28, run9)：按版本 id 恢复后正文 A 回归、AI 建议消失、DB content.body 确为 [A]；生产 smoke | — |
| NOV-R-001 | 无审阅证据时不伪造评分 | `Review.tsx` | run review outputs | 已验收 | E2E 主线④；生产空态 smoke | — |
| NOV-R-002 | 展示七维、一致性、连续性和问题证据 | `Review.tsx` | review/consistency nodes | 已验收 | protected E2E 1 passed (2026-07-28, run 8f1fd62b, 19 ai_calls)；protected-06 截图；生产 smoke | — |
| NOV-R-003 | 审阅建议先预览再由用户应用 | `Review.tsx` | repair preview/apply API、签名与并发门禁 | 已接线 | Repair 定向 15 passed；前端预览/确认 2 tests；后端全量 781 passed；浏览器验证无密钥时显式失败且不改正文；提交 `6f7184c` / Actions `30447533339` 五项全绿 | 真实 Provider 正向预览→应用 |
| NOV-S-001 | BYOK 只保存在当前会话 | `Settings.tsx` | 请求 Header 优先 | 已验收 | E2E 主线④；生产请求验证 | — |
| NOV-S-002 | 创作知识导入、导出和统计 | `Settings.tsx` | knowledge/stats API | 已接线 | 页面/API 接线 | 真库数据操作 E2E |
| NOV-S-003 | 修改密码 | `Settings.tsx` | auth password API | 已接线 | 页面/API 接线 | 正负例 E2E |
| NOV-Q-001 | 单元、构建和确定性主链门禁 | Vitest、Playwright | 真 PostgreSQL/FastAPI | 已验收 | 提交 `07a8c0f`：本地后端 761 passed / 9 skipped / 1 xpassed、前端 12 passed、build、E2E 4 passed / 4 skipped、三项静态校验；Actions `30439322188` 五项全绿 | 后续批次继续维持同提交 CI |
| NOV-Q-002 | 真实 AI 新 UI 全链 | protected Playwright | DeepSeek、run/ai_calls | 可用 | protected “小说主线⑤” 1 passed (5.2m)；run `955d4719-8e21-4043-8a3e-2352c06c0ce2` 20/20 nodes、22 succeeded ai_calls；Writer 含 Prompt Compiler 三层指令，最终一致性含五维读者体验；提交 `5c544ff` / Actions `30445384633` 五项全绿 | Repair Engine 正向 Provider 预览应用；最终生产 smoke 后提升已验收 |
| NOV-D-001 | 当前版本推送和部署 | GitHub Actions、Docker | 生产基础设施 | 可用 | `7851b7f` 已推送并部署到 `novel.xyjin.xyz`；迁移到 `nc_v7_novel_project_mapping`；生产 smoke 15/15 | 生产真实 20 章双轨与人工盲评不属于部署 smoke |

## 更新规则

- 修改需求或实现时，同一批次更新本表。
- 状态提升必须在“当前证据”中写入命令、测试、run、提交或生产验证。
- 只存在代码、路由、按钮或类型定义时，最高为“已接线”。
- 失败或跳过的测试不能写成通过。

## 2026-08-02 V6/V7 质量合并追踪补充

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| V7 章节交接契约与 V6 contents 桥 | 可用 | `backend/app/v7/integration/v6_bridge.py`；新增质量回归通过；仅质量通过章节写入 V6 | 真实数据库写回、书库/编辑器/导出端到端 |
| V7 85 分跨章质量门 | 可用 | `backend/app/v7/integration/quality.py`、StoryDirector 二次复核与最多两次重写 | 真实 Provider 多章长跑、人工盲评 |
| V6 主链最终人文化 | 已接线 | `chapter_loop.py` 在修复/重规划后调用真实 `bootstrap.final_humanize` 并做最终 review | 真实 Provider/数据库环境复测 |
| V6 事实冲突局部修复 | 已接线 | `write_fact_reconcile` 返回精确修复项；主链应用、二次审查、失败转 `needs_review` | 真实冲突样本与回滚/写回证据 |
| V6/V7 成本与 Prompt provenance 统一 | 可用 | `UnifiedAIGateway` 收敛 Provider transport；V6/V7 写 `ai_execution_ledger`；V7 `ensure_runtime_version` + `record_runtime_execution` 写 Prompt provenance；67 项目标回归通过 | Alembic migration、真实播种/回放、V6/V7 账本对账和生产长跑 |

## 2026-08-02 本地收口证据更新

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 统一 Provider transport（V6/V7 sync/async/stream） | 可用 | `unified_gateway.py`；统一 Gateway 回归、流式 SSE 回归通过；E2E 真实后端运行 | 真实 Provider 多章回放 |
| Prompt provenance 与 runtime seed | 可用 | `alembic current` = `nc_v6_v7_runtime_ledger (head)`；seed 8 个 runtime Prompt，重复执行幂等 | 真实 Provider 执行记录与生产审计 |
| 跨版本成本账本 | 已接线 | `ai_execution_ledger` migration、V6/V7 写入、项目范围 `/ledger` 和日期/任务统计回归 | 真实 V6/V7 回放对账、生产成本核对 |
| V7 → V6 章节质量桥 | 可用 | 85 分质量门、`transition_contract`、幂等 contents bridge、V6 二次复核回归 | 真实生成后书库/编辑器/导出链路 |
| 生成质量验收 | 已接线 | 全量后端 843 passed；E2E 18 passed/9 skipped；20 章脚本 dry-run 可执行 | 真实 20 章双轨、跨章指标、去 AI 味差分、两名编辑盲评 |
| 强制 AI development gate | 已接线 | AST 真值、交付声明、空白检查通过；强制脚本 exit 3 的宽泛告警已在 KI-005/015 解释 | 规则清零或完成 CI 级告警收敛 |

## 2026-08-02 继续整改追踪补充

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| ai-workbench 参考落地 | 可用 | `docs/AI_WORKBENCH参考评估_20260802.md`；情绪目标、钩子、分层去 AI、读者体验已接入 V7/V6 提示与契约 | 真实长篇与人工盲评 |
| V7 读者体验证据 | 已接线 | V7 Review 强制五项字段并持久化到 transition contract；目标与全量回归通过 | 真实 Provider 样本的人感相关性 |
| V7 novel→V6 project 映射 | 可用 | `nc_v7_novel_project_mapping`；本地回填 6994 条；跨 project pair 拒绝测试通过 | 生产迁移与真实书库/编辑器/导出回写 |
| Prompt 管理权限 | 可用 | V7 Prompt router 使用 admin read/write guard；权限结构测试通过 | 双用户生产接口回归 |
| 当前质量回归 | 可用 | 后端 843 passed/138 skipped/1 xpassed；前端 32 passed；E2E 最新复跑 17 passed/10 skipped | Provider 20 章双轨、人工盲评 |

## 2026-08-02 真实 Provider 20 章双轨更新

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| V6/V7 真实双轨自动回放 | 可用 | 真实 DeepSeek、本地 PostgreSQL/Redis/Celery；两轨各 20/20；V6 平均 79.6、V7 平均 92.0 | 两位独立人工盲评 |
| 跨版本成本账本 | 可用 | `ai_execution_ledger` 369/369 成功、0 失败、3.190506 元；V6/V7 分项可对账 | 目标部署环境成本核对 |
| Prompt provenance | 可用 | V6 7 个、V7 6 个 Prompt identity，版本、usage、task type 均可追溯 | 生产审计回放 |
| V7→V6 书库/编辑器/导出 | 可用 | 20 章 `contents`、mapping、编辑器、完成度、TXT/Markdown/EPUB 真实接口证据 | 目标部署环境回放 |
| 人工盲评 | 已接线 | 20 个匿名 case 和评分模板已生成 | 0/20 case 达到两位评审 |
| 生成质量目标 | 已接线 | 自动连续性、审稿、去味和重复风险指标已生成 | 人工评分及人感差异报告 |

## 2026-08-02 生产部署证据

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 当前提交生产部署 | 可用 | `7851b7f`；Docker 应用容器重建；迁移 head；公网 healthz 200 | 生产 20 章质量长跑 |
| 生产用户入口 | 可用 | 生产 smoke 15/15；生产 Playwright 走查 4/4 | 真实 Provider 生成质量和人工盲评 |
# 2026-08-21 `lengdu` 方法清洁室融合矩阵（已部署，单章真实骨架已验证）

## 2026-08-24 全文生成与三章长跑最新追踪

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 全文生成预算重写不截断、不越界 | 已接线 | `b17931e` 校准 DeepSeek 预算重写余量；`b0c43cb` 增加小场景完成空间；生产定向回归 `111 passed` | Provider 字符边界不是硬约束，场景级预算分配需重构 |
| 生成期轻度表达风险不误阻断 | 已接线 | `4d93819` 增加 `scene_metaphor_density_warning`，轻度类比不再阻断，明显密集仍重写 | 真实多题材样本人工可读性 |
| 清空历史后三章真实 Provider 长跑 | 已接线 | `813095b5`、`adb8d0ba`、`19d3219a`、`ddbd4d50` 均从 `active_chapters=0` 启动；均 fail-closed 停止，源作未改 | 章节级预算分配/整章生成路径修复后再验收三章 |
| 20章真实 Provider 长跑 | 未开始 | 用户已将当前目标收敛为先三章；未达到三章通过证据 | 三章通过、人工盲评和发布门禁 |
| GitHub 推送 | 已接线 | 本地提交 `b17931e`、`4d93819`、`b0c43cb`；生产运行时代码已核对 | 工作站到 GitHub HTTPS 推送20秒超时，未验证远端包含本轮提交 |

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 作者意图优先、已确认事实优先 | 已接线 | `authoring.chapter_skeleton 1.2.0` Prompt；不向 Writer 暴露外部检测分数 | 真实 Provider 多样本人工评审 |
| 角色受限选择与信息后果 | 已接线 | 场景契约增加 `trigger/choice/cost/visible_change`；确定性协议校验 | 真实骨架是否能支撑作者成稿 |
| 读者体验在生成期显式化 | 已接线 | `reader_experience_plan` 包含开场抓手、发现、期待变化、余波、下一章问题 | 不将计划字段误报为读者盲评或朱雀通过 |
| 原稿与 AI 候选隔离 | 可用 | 仍独立写入 `versions(entity_type=chapter_skeleton)`，正文不变；`1a150ff` 已部署 | 新代码真实账号回归 |
| 场景链在编辑器可见 | 已接线 | 前端展示场景链、人物变化、伏笔动作、待人工确认事实；`1a150ff` 已部署 | 生产浏览器登录态视觉验收 |
| `lengdu` “10篇9篇过朱雀”复现 | 已接线 | `a0695ce` 线上单章真实 `deepseek-chat` 返回856字、6场景、读者计划5字段并通过结构/预算门禁；未做朱雀 | 多样本人工可写性、正文样本和外部朱雀报告 |

## 2026-08-24 整章预算压缩与三章真实 Provider 验收

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| 整章生成不因中文字符边界反复超长/截断 | 可用 | `CHAPTER_SINGLE_PASS_GENERATION_VERSION=2.50.0`；超长或“超长+截断”候选走独立终稿压缩；生产相关回归 `119 passed` | 不同题材、多模型字符边界的长期统计仍待积累 |
| 清空历史后连续生成三章 | 已验收 | 全新副本 `6cb195ed-c0e9-41fe-9cc5-c4a15cf1c066`，`active_chapters=0`；3/3 完成，正文 2938/2683/2905 字，V7 89/87/87 | 两位人工盲评、朱雀外部检测 |
| 20章真实 Provider 长跑 | 未开始 | 用户要求先做三章；当前仅完成三章 generation scope | 20章连续性、成本、人工盲评和发布证据 |
| 七道发布门禁、AI披露人工确认、正式发布闭环 | 未开始 | 本次使用 `--acceptance-scope generation`，未运行 publication scope | 七道门禁、披露确认、平台定稿与发布回执 |
| GitHub 远端包含本批代码与文档 | 已验收 | 提交 `e1313b0` 已推送到 `origin/agent/publishing-v0.9.2` | CI 结果仍需按远端工作流单独查看 |

## 2026-08-21 作者主导章节骨架补充矩阵

| 需求 | 状态 | 当前证据 | 未闭合门禁 |
|---|---|---|---|
| AI生成700–1000字章节骨架 | 可用 | 生产真实 DeepSeek 样本813可见字；Prompt `authoring.chapter_skeleton 1.1.0`；服务端硬校验可见字数700–1000 | 多样本可读性、连续性和作者评审 |
| 骨架不覆盖正文 | 已接线 | 独立 `versions(entity_type=chapter_skeleton)`；接口返回“正文未修改”；前端无整章默认入口 | 生产浏览器回归 |
| 人工修改并保存骨架 | 已接线 | `skeletons/save` 另存 `skeleton_human_edit`；前端保存按钮和范围提示 | 真实账号操作验收 |
| 编辑器体现人工主导 | 已接线 | `ChapterSkeletonPanel.tsx`、顶部“AI辅助创作·人工成稿”、移除整章候选按钮 | 生产视觉验收 |
| 人物/剧情/伏笔/世界观作为生成上下文 | 已接线 | 当前小说 Bible、V7 state、plot threads、foreshadowings 查询；世界观标题去重 | 真实作品资料完整度验收 |
| 章节骨架质量与读者可写性 | 已接线 | 生产单样本813字已持久化；单样本只证明技术链，不证明质量 | 多样本 Provider + 作者人工评审 |

### 2026-08-21 章节骨架生产验证补充

| 项目 | 证据 | 结论 |
|---|---|---|
| 部署 | `0c95ae6`；healthz `code=0`；生产 Prompt `1.1.0` | 技术链可用 |
| 首轮失败根因 | 422 为请求头错误；修正后 Provider 返回466字并被502硬门禁拒绝 | 未伪造成功，已修复提示词预算 |
| 修复后真实样本 | DeepSeek `deepseek-chat`，813可见字，`provider_verified=true`，版本 `f01cd7df-6dad-4a42-9d2f-770a127f85eb` | 单样本可用 |
| 正文保护 | 正文 md5 `790ae86e56200e2291496ffae4a761cf`，长度16602，前后不变 | 生成与正文隔离 |
| 尚未闭合 | 浏览器登录态视觉验收、骨架质量多样本/人工评审、朱雀与20章长跑 | 不能宣称质量验收 |
