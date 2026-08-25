# Starlume AI — 真实进度

## 2026-08-25 旧失败的修复后单节点恢复（`3fed1a4` 已部署）

- 生产旧run `d74db417-83db-4be9-b57c-6c2dde4b006e` 属于2.52部署前的2803字压缩终稿误拒绝，不是新版反复失败。
- 只对该类“终稿字数已合法、旧版截断标记误拒绝”的精确签名开放恢复；质量失败、真实超长和2.52新失败仍禁止原样重试。
- 进度页新增“使用修复版重试章节”，复用已成功的12个步骤并隐藏全流程重执行；用户确认前不会调用真实Provider。
- 本地后端2项、前端11项、生产构建和强制门禁通过；提交已推送并部署，公网API/Worker/Frontend健康。
- 部署后旧run与节点仍为failed、attempt=1，本批没有生成正文。当前只能标记为“恢复入口可用”，不能标记该章、三章/20章、朱雀或发布闭环完成。

## 2026-08-25 整章压缩终稿高频误失败修复（`114bee6` 已部署）

- 已定位生产 run `d74db417-83db-4be9-b57c-6c2dde4b006e`：首稿3540字、压缩终稿2803字，字数已在2200–3000内，却因 `finish_reason=length` 被旧逻辑拒绝。
- 修复版本2.52.0以DeepSeek默认1.50汉字/token和首稿实测比例计算token上限；不再把2550字等同2550 token，也不再强制压缩轮至少1900 token。
- 只有第二次压缩稿字数合格且完整句末、引号/括号闭合时，`length`才作为可观察warning继续后续质量门禁；残句、逗号/省略号结尾继续失败。
- 生产同型离线回放及反例、生成质量、bootstrap可靠性和canonical链合计147项通过；本批没有重复调用真实Provider。全量后端因本机认证注册返回503而不能标记通过。
- 提交 `114bee6` 已推送并部署；生产API/Worker均确认2.52.0，容器整章回归8项通过，公网healthz正常。只保留最新源码回滚包和最新数据库备份；清理3.286GB构建缓存后磁盘占用30%。
- 当前边界：本批没有调用真实Provider、没有生成测试小说或章节；本项不等于三章/20章、朱雀95/5/0、披露或发布闭环验收。全量后端仍因本机注册503环境阻断。

## 2026-08-21 `lengdu` 方法清洁室融合（`a0695ce` 已部署，单章真实骨架已验证）

- 复用现有章节骨架入口，不新增正文长跑或整章重写流程；Prompt `authoring.chapter_skeleton` 从 `1.1.0` 升级为 `1.2.0`。
- 生成协议新增作者意图/硬事实优先、角色受限选择、信息必须产生行动或代价变化、避免单向答案通道和伪造人味细节；Writer 不接收朱雀/AIGC分数。
- Provider 结构新增 `reader_experience_plan`，场景节点要求 `trigger → action → choice → conflict → cost → outcome → visible_change`；后端确定性协议门禁缺字段、占位词、重复结果直接失败，正文不写入。
- 编辑器骨架工作区展示读者体验目标、场景链、人物变化、伏笔动作和待人工确认事实，作者仍可独立修改保存骨架，正文真相源不变。
- 本地证据：章节骨架契约 `8 passed`，前端 `58 passed`、`npm run build`、真实性/交付声明/强制开发门禁通过；融合/骨架集成测试因本机 PostgreSQL 未启动而注册返回 `503`，不能标记全量通过。
- 生产证据：`a0695ce` 已提交、推送、部署；备份 `backups/pre-deploy-a0695ce-20260821-112435.sql.gz` gzip 校验通过，292M；迁移 `nc_starlume_authoring (head)`，线上 Prompt `1.4.0`，healthz、容器和未认证 authoring 路由检查正常。
- 单章真实 Provider：HTTP 200，`deepseek-chat`、`provider_verified=true`，骨架856可见字、6个场景、读者计划5字段，版本 `899f2b41-9dc9-4a46-a626-232edfade4e9`；正文哈希前后相同。
- 当前状态：章节骨架单章真实链路**可用**；多样本人工可写性、正文质量、朱雀样本、三章/20章长跑、AI披露人工确认和正式发布闭环仍未开始/未验收。

## 2026-08-21 扫榜书名自动应用修复（`9ffcd96` 已部署）

- 现象：扫榜选题已经有生成书名，进度页仍进入 `human_confirm_title`，用户需要再次选择；生产当前等待态 run `d7147027-10aa-41c5-9ca7-e3caabfe6071` 的 `source_type=ranking_topic` 已确认属于该问题。
- 根因：扫榜接口未传 `auto_confirm_title=True`；工作节点在自动确认分支中又优先使用 `plan_idea` 首个候选；历史等待态没有自动修复入口。
- 修复：扫榜两个建书入口传入自动确认标记；`create_run()` 显式保存 `selected_title`；Bootstrap 优先使用显式扫榜题名；前端刷新时对旧扫榜等待态调用现有 n2 确认 API 自动推进。普通灵感流程仍然保留人工选名。
- 本地证据：后端扫榜/工作流/契约回归 `29 passed, 1 warning`；前端全量 `58 passed`，Progress `9 passed`；前端生产构建、Python 编译、AI真实性、交付声明和 diff 检查通过。包含真实 Celery/Redis 的完整 V2 集成组因本机 Redis 未启动而未标记全绿。
- 生产证据：`9ffcd96` 已推送并部署；备份 `backups/pre-deploy-9ffcd96-20260821-095552.sql.gz` gzip 校验通过（约289M），公网 healthz、迁移和全部正式容器正常。现有 run `d7147027-10aa-41c5-9ca7-e3caabfe6071` 已由正式确认路径自动应用扫榜书名，`human_confirm_title=succeeded`，当前进入 `generate_story_arc`。
- 状态边界：扫榜书名自动应用为“可用”；三章/20章真实 Provider 验收和发布闭环仍不受本修复影响。

## 2026-08-20 朱雀报告驱动的生成期修复（`9e49213` 已部署）

- 外部报告事实：本章人工特征 0%、疑似 AI 68.35%、AI 特征 31.65%；片段1 AIGC 0.8575，片段2 AIGC 0.9984。报告暴露的不是单个词，而是重复身体状态/系统信息、同构段落和赶路流水账。
- 生成期新增三类拦截：`scene_state_echo`（多个身体/任务/规则状态被重复解释）、`scene_procedural_motion`（移动段像行程记录且事件落点过晚）、`scene_subject_opening`（无对白移动段连续以同一主语起笔）。同时收紧“因为/所以+整齐理由”和不确定性清单式解释。
- 重试改为专用路径：状态回声只保留第一次呈现并写变化；移动段压缩无事件路线并提前落阻碍/线索/他人介入/选择代价；主语重复改从动作、物件、声音、环境后果或他人反应起笔。检测证据只传类别和统计，不回传失败原文，避免 Provider 模仿坏样本。
- 生成协议升级为 `generation-style-protocol-v3`，V7 场景生成版本升级为 `2.32.0`；不改正文真相源、不新增迁移、不启动长跑。
- 本地证据：生成质量定向 `81 passed`；相关 V7/质量/预算/文本/视角契约 `131 passed`；后端全量 `1190 passed, 138 skipped, 1 xpassed, 2 warnings`；AI真实性、交付声明、强制开发门禁和 `git diff --check` 通过。
- 生产证据：`9e49213` 已推送并部署；备份 `backups/pre-deploy-generation-naturalness-184604.sql.gz`（gzip 校验通过，约288M），生产版本为 `2.32.0`，Alembic、API/Worker/Beat/Frontend/PostgreSQL/Redis 和公网 healthz 均正常。
- 验收边界：本批没有再次调用 DeepSeek，也没有做新的朱雀报告；当前只能标记为“生成期修复已部署”，不能标记为朱雀95/5/0或三章/20章验收。

## 2026-08-20 生成期修复后单章真实 Provider 复验（未通过）

- 在生产创建全新隔离副本 `5adba896-6be3-4cdd-a627-2ecd4033a82d`，启动前 `active_chapters=0`；源作品 `4ee9db30-98c7-40d5-9484-12432efed69e` 未被清空或写入，仍为10章。
- 使用生产真实 `deepseek-chat` 只生成第1章一次，run=`0f683f64-7ed0-4167-8f28-81abe0105c30`，章节=`74e36c14-6c57-4760-9833-769cbe87d895`；生成正文报告字数 `3532`，最终 `status=needs_review`、`quality_gate.score=87`，不是 `completed`。
- 真实阻断原因为 V7 节奏质量门禁：中段弟子求助部分节奏稍缓，与前后紧张感衔接不够紧凑；同时该副本预算为推荐 `2000–2700`、最大 `3000`，本次结果报告字数超过最大值。脚本按 fail-closed 停止，没有进入七道发布门禁，也没有调用第2章。
- 新增的生成期自然性结构统计本次未命中 `scene_state_echo`、`scene_procedural_motion`、`scene_subject_opening`；状态回声组数为0，移动流水账信号未达门槛，主语段首比例约0.19。说明本次主要失败点已经从报告中的结构重复收敛到节奏与章节预算，不能据此宣称正文已达朱雀95/5/0。
- 当前证据：真实 Provider 单章已执行但未通过；隔离副本保留1个 `needs_rewrite` 章节用于诊断，原作数据未改变；未再次提交朱雀检测，三章/20章长跑仍未开始。

## 2026-08-20 单章退化根因修复（`dd78b68` 已部署，未重新调用 Provider）

- 复盘 `5adba896-6be3-4cdd-a627-2ecd4033a82d` 的真实正文和场景计划后确认：计划把中段弟子求助明确写成“放缓节奏”并分配约704字；正文又在门缝、光、灰线、借期之间重复回放，导致主线推进变慢。这是生成计划和写作契约问题，不是朱雀分数问题。
- 修复一：长章节计划契约拒绝“中段放缓/舒缓/缓冲”的独立节拍，Provider 只能把辅助互动接到主线的风险、资源、关系、位置或线索变化上；Writer 协议同步升级为 `generation-style-protocol-v4`。
- 修复二：自然性统计从原文计算对白数量，不再先删除对白后得到恒定 `dialogue_count=0`；避免对话场景被误判为无对白移动流水账。
- 修复三：章节读者预算重新作为唯一硬上限，取消“最后场景自然波动”把3000字章节放大到3672字的放行路径；场景预算仍是分配提示，不成为额外硬门禁。
- 本地证据：相关生成/质量集合 `133 passed`，Python 编译、`git diff --check`、AI真实性、交付声明和开发门禁全部通过。
- 生产证据：`dd78b68` 已部署，备份 `backups/pre-deploy-mainline-pacing-dd78b68-20260820-120013.sql.gz` gzip 校验通过；生产 healthz 的数据库、Redis、Worker、DeepSeek、Tavily 均正常。
- 边界：修复后尚未重新生成正文，也未重新提交朱雀；上一次单章仍是 `needs_review`，不能宣称质量已恢复、朱雀95/5/0通过或三章/20章开始。

## 2026-08-20 编辑器可读性与本章上下文修复（`c851ed5` 已部署）

- 根因一：编辑器正文使用了深色主题文字变量，但浅色编辑画布是白底，导致正文接近白色、几乎不可读。已将编辑器画布、正文、工具栏和辅助文字改为作用域化的明暗主题变量，并保留深色主题覆盖。
- 根因二：右侧上下文只读 `knowledge_items`，且把 `content_id IS NULL` 的项目级记录混入章节；没有读取当前作品的 V7 角色/剧情/世界状态、`plot_threads` 和 `foreshadowings`。已改为当前项目+当前章节精确范围，并确定性合并当前章节的 V7 状态、剧情线、伏笔和已确认创作圣经。
- 根因三：编辑器仍暴露“重新生成/生成下一章”等自动生产入口，与人工主导辅助创作定位冲突。已改为“续写候选/选区润色/整章候选”，AI只提供候选，正文由作者确认；右侧改名为“AI 共创助手”，明确先讨论再生成。
- 本地证据：前端 57 tests、后端全量 `1188 passed, 138 skipped, 1 xpassed`，lint/build、`git diff --check`、AI真实性与交付声明门禁通过。
- 生产证据：`c851ed5` 已推送并部署；备份为 `backups/pre-deploy-editor-authoring-20260820-181757.sql.gz`，Alembic 为 `nc_starlume_authoring (head)`，公网 healthz 返回 `ok`，API/Worker/Beat/Frontend/PostgreSQL/Redis 健康。浏览器登录态页面验收待账号输入确认后补做。
- 边界：20章真实 Provider 长跑仍为**未开始**；三章真实 Provider 仍为**未通过**，本批不重复调用 Provider。

## 2026-08-20 产品边界澄清：三章长跑仅是验收实验，不是产品工作流

- Starlume AI 当前产品定位仍是“人工主导、AI辅助创作”：作者提供灵感、事实、取舍和最终确认，AI提供大纲/细纲、场景候选、实时对话、连续性追踪和质量建议；编辑器不会因为这次验收而默认自动写长篇。
- 本轮三章调用是此前约定的真实 Provider 验收实验，只用于验证生成链的连续性和失败边界，不是20章自动生产，也不应作为日常创作入口。20章长跑继续保持“未开始”。
- `84e3957` 部署后的新空副本 `8457e31c-7f6d-4a85-b190-b1da19cad9ad` 已清空历史（`active_chapters=0`）；真实 DeepSeek 第1章通过（2655字、自动评审91、连续性通过），第2章返回500并停止，第3章未调用。该次失败没有新的持久化章节2 run/event，当前只保留 trace `9f872ddf7b9045e3934c293758471402`；在补齐可观测根因前不再重复 Provider 长跑。
- 运行结论：三章验收仍为“未通过”，但不改变辅助创作产品方向；下一步应优先完成编辑器内的人工确认、AI实时对话、上下文可视化和候选应用闭环，再单独安排有明确诊断证据的验收实验。

## 2026-08-20 M7 `0d0f266` 生产三章复验结果与下一根因（本地修复待部署）

- `0d0f266` 已部署后，新空副本 `501ff058-e378-440d-a23b-3cacea51d3fc` 启动前 `active_chapters=0`；真实 DeepSeek 第1章通过：3106 字、自动评审 92、V7 `v7_quality_gate_passed`、连续性 `continuous`。第2章真实调用后在第2场两次生成均命中 `repeated_paragraph_opening`，API返回500并停止；第3章未调用，副本不续跑。
- 新根因：本次不是早期爽点、章节预算、跨场景交接或对白破折号问题，而是短场景候选自身 8 段以上、同一姓名段首至少4段且占比至少45%的生成期自然性门禁；两次通用风格重写未有效重排段首。该门禁仍保留，不把真实重复降级为通过。
- 已本地接线最小修复：对 `repeated_paragraph_opening` 使用专用生成期重排指令，明确禁止连续同姓名段首和超过四分之一段首，要求从动作/物件/声音/环境后果/对白/他人反应起笔并保留事实；版本升级为 `SCENE_SERIAL_GENERATION_VERSION=2.31.0`。本地尚未全量回归、提交、部署或重新调用 Provider。

## 2026-08-20 M7 最新根因修复（本地已接线，待全量/提交/部署）

- 生产 `78db748` 的全新空副本 `adb68f72-6532-460f-856a-eea8fb07e71a` 启动前为 `active_chapters=0`；真实 DeepSeek 第1章生成 3569 字、连续性 `continuous`、自动评审 90，但在正文完成后被 V7 `payoff_strength_insufficient` 阻断：前3章策略要求 `peak`，Provider 计划只给出 `high`。第2、3章未调用，源作品未修改。
- 根因不是 Provider 调用失败，也不是字数/连续性问题，而是“早期爽点最低档位”只在正文生成后校验，弱计划已经消耗完整正文调用。该章计划还把 `payoff_intensity=high` 与系统绑定/任务出现混在一起，生成前没有要求把可见结果、主动选择、反馈和余波落实成峰值动作链。
- 已本地前移为生成期修复：`SceneDirector` 在场景计划通过后、正文 Provider 调用前校验显式爽点契约；低于当前策略 floor 时，只调用一次真实 Provider 修复契约和 beats，禁止只改标签、禁止新增剧情事实；修复仍不达标则在正文前 fail-closed。计划 Prompt 同步明示前3章的实际执行要求，版本升级为 `SCENE_SERIAL_GENERATION_VERSION=2.30.0`。
- 新增生成前峰值爽点回归；当前定向质量集合 `87 passed`。本地全量、提交、生产部署和新空副本三章受控复验尚未完成；不得把本地修复写成已上线或把最新三章写成通过。

## 2026-08-20 M7 最新真实 Provider 复验（已部署但未通过）

- `26180a3` 生产复验的新空副本 `d41481ba-ec72-4d98-bd9a-eb9724a5f123` 第1章已通过：2811 字、自动评审 91、V7 质量门禁通过、连续性 `continuous`；第2章第1场因 `scene_metaphor_density` 阻断，第3章未启动。
- 新根因：第2章第1场真实候选 1256 字，命中 11 处非对白类比，生成阈值为 7；两次生成期修复仍未收束。修复方向不是放宽类比门禁，而是让自然性重试统一切换到 `plain_factual` 生成路径，继续要求非对白类比为 0。
- 本地版本升级为 `SCENE_SERIAL_GENERATION_VERSION=2.29.0`；本轮未再调用 Provider，待定向、全量回归后部署，再决定是否继续一次新空副本复验。

- `57c4062` 已提交并推送，生产已部署并健康运行。新空副本 `3186392a-fe5d-4ab7-990b-86c3654c2843` 启动前 `active_chapters=0`，第1章未落正文，因 `dash_density` 生成期门禁失败停止；第2/3章未启动。
- 新根因：`deai_metrics` 的章节级破折号密度把对白中的合法打断也算进生成期重试，短首场因此被判 `dash_density`。已本地修复为只在叙述部分（去除完整引号对白后）仍超过 5/1000 时重试；对白破折号仍保留后置审计证据。版本升级为 `SCENE_SERIAL_GENERATION_VERSION=2.28.0`，新增对白破折号回归测试。
- 定向测试 `100 passed`，后端全量 `1187 passed, 138 skipped, 1 xpassed, 2 warnings`；待提交、部署和最后一次全新空副本三章受控复验。

- `61ea900` 已提交并推送；生产已部署且健康。全新副本 `b23be4ec-b27a-41f0-9f4c-1c061a079d19` 启动前 `active_chapters=0`，真实 DeepSeek 第1章已通过：2878 字、自动评审 89、V7 质量门禁通过、连续性 `continuous`；第2章在生成期阻断，第3章未启动。
- 新根因：第2章第3场 `accepted=2465,candidate=872,future_min=0,chapter_max=3192`，长度超出 145 字仍处于最后场景 480 字自然波动内，但同时命中 `repeated_paragraph_opening`。生产日志显示这是“已接受场景链的主角段首比例”与短收束场景合并后触发的交叉误报，不能把一次自然交接段落当作场景本身重复。
- 已完成本地最小修复：跨场景段首风险只有在当前候选自身也重复同一姓名段首（至少 3 段且占比不低于 45%）时才进入生成重试；候选自身的短场景重复检测仍保留。版本升级为 `SCENE_SERIAL_GENERATION_VERSION=2.27.0`，新增交接段落回归测试；先跑定向、全量，再部署并以新空副本做一次受控复验。

- 最新生产提交为 `aadb354`，已完成 Docker 重建、迁移和健康检查；生产 Alembic 为 `nc_starlume_authoring (head)`，公网 healthz 返回 200，API/Worker/Beat/Frontend/数据库/Redis 均健康。原生产脏文件未清理。
- 在全新副本 `0a1e6ea2-3634-4680-a3b7-7001a20d74a4` 上执行真实 DeepSeek 三章目标；启动前 `active_chapters=0`、历史章节已清空，原作未修改。第1章未落正文，第2/3章未启动。
- 新阻断根因：第1章第3场已接受 2269 字，候选 1276 字，后续场景最低需求为 0，生成期章节上限为 3192 字；候选完整但超过章节总预算，有限重试后命中 `scene_chapter_budget_overrun`。这不是场景读者目标的第二硬门禁，而是最后场景的有界自然波动额度不足。
- 已完成本地最小修复：将最后完整场景的自然波动额度从 192 调整为 480 字，仍要求无后续场景、正文完整、不得接近平台级上限；版本升级为 `SCENE_SERIAL_GENERATION_VERSION=2.27.0`，新增 3545 字边界和交接段落回归测试。定向测试 `108 passed`，后端全量 `1186 passed, 138 skipped, 1 xpassed, 2 warnings`；待提交、部署和新空副本单次复验。
- 当前结论：三章真实 Provider **未通过**，20章真实 Provider **未开始**；本次失败后不续用副本、不重复盲跑。披露人工确认、两位盲评、七道门禁完整发布闭环仍未验收。

- `d28a09f` 已提交、推送并部署；生产备份为 `backups/pre-deploy-density-fix-20260820-150320.sql.gz`（gzip 校验通过），Alembic 为 `nc_starlume_authoring (head)`，公网 healthz、容器和新 authoring 路由均正常。
- 按“先清空历史再长跑”创建全新副本 `b3cc7b91-74c6-4dd6-a122-aa2a003eea7a`；启动前 `active_chapters=0`、V7 状态为 0，源作品未修改。使用真实生产 DeepSeek 执行目标三章，未继续使用已有章节副本。
- 真实结果：第 1 章生成正文 3320 字，连续性 `continuous`、自动评审 91 分，但 `v7_quality_gate_failed`，原因是“动作开场”门禁把前 100 字内进入抬手处置的短环境引子判为 `unknown`；开场修复 Provider 又因严格单段契约报 `AIGatewayError`。第 2、3 章未启动，副本只保留第 1 章 `needs_rewrite`。
- 已定位并本地修复：仅对“原首句未分类、前 160 字内出现可见动作”的短环境引子按动作开场处理；真正静态开场、动作过晚、身体疼痛模板仍阻断。定向回归 `97 passed`，最终后端全量 `1184 passed, 138 skipped, 1 xpassed, 2 warnings`；修复仍待提交、部署和新空副本复验。
- 第二次全新副本复验仍暴露两个具体规则缺口：正文第1章 2929 字、连续性通过、自动评审91，但普通架空冲突词“杀人”被全局敏感词阻断，且“捏/拍”未进入动作词表导致开场仍判 `unknown`。已本地改为架空题材非露骨冲突仅告警、现实/露骨敏感内容继续阻断，并补齐常用动作词；定向集合 `96 passed`，尚未部署。
- 第三次全新副本在部署后未落正文即暴露 `scene_reader_budget_overrun[accepted=674,candidate=2153,future_target=778,planning_max=3000]`；根因是场景串行器仍把读者目标预算当第二个硬上限。已本地改为低优先级 `scene_reader_budget_variance` 告警，章节总预算和后续场景最低表达空间仍为硬门禁；待测试、提交、部署后再做最后一次新空副本复验。
- 当前结论：三章真实 Provider **未通过**，20 章长跑**未开始**；不把自动 91 分、单章正文或生产健康状态解释成三章/人工质量验收。

## 2026-08-20 辅助创作控制面本地实现（M1–M6 已接线/可用）

- 新增持久化 AI 会话、确定性编辑器上下文、最小故事 Bible、GPT/DeepSeek/豆包角色路由、豆包真实适配器、章节级写作事件和人工发布回执；正文仍以 `contents` 为唯一真相源。
- 新增迁移 `nc_starlume_authoring`，本地已升级到 head；新增五张控制面表，不删除原始正文。前端编辑器已显示上下文、Provider 状态并恢复会话。
- 本地证据：后端全量 1177 passed、138 skipped、1 xpassed、2 warnings（辅助创作契约 5 passed）；前端 57 passed、TypeScript 检查和生产构建通过；会话/角色/三章登记 API 集成返回 200。
- 本批已部署生产提交 `0ae3875`；备份为 `backups/pre-deploy-authoring-20260820-143646.sql.gz`，迁移 head 和公网 healthz 已验证。三章真实 Provider、两位独立盲评、披露人工确认、七道门禁和正式发布仍未验收；本批没有执行真实 Provider 长跑。

## 2026-08-20 三章真实 Provider 一次性尝试（阻断，修复待部署）

- 在生产提交 `0ae3875` 上使用真实 DeepSeek、空副本 `11203278-a624-4c7c-9278-d3a304806be0` 执行三章脚本；启动前 `active_chapters=0`、`v7_states=0`，第1章未落正文，原作未修改。
- 第1章第3场在生成期阻断：已接受 2128 字 + 候选 1494 字 > 章节上限 3192 字；根因是 Prompt 没有显式呈现当前剩余章节额度。
- 本地已修复为 Prompt 明示剩余额度硬边界，并新增回归测试；部署后只复验一次，失败即停。

## 2026-08-20 辅助创作转型契约与 GitHub 改名 [commit 3c376a0 已验收]

- 主仓库从 `Catatlina/NovelCraft-Personal-Studio` 原地改名为 `Catatlina/starlume-ai-studio`，仓库 ID `R_kgDOTT8enQ` 保持不变；`Catatlina/starlume-ai` 是另一历史项目，未修改。
- `3c376a0` 已推送至 `agent/publishing-v0.9.2`，新增 `docs/Starlume-AI-开发文档/` 10份权威文档，覆盖产品需求、架构、开发路径、AI质量门禁、AI遵从、实施、交接、码字溯源和决策记录。
- 现行硬约束：复用现有编辑器、Gateway、V7、`contents`、版本和发布准备；按最小垂直切片开发；AI结果均为候选；正式设定、正文和发布必须人工确认。
- 强制门禁已切换到 Starlume AI 新规范。`git diff --check`、AI真实性、交付声明和 `bash scripts/ai_development_gate.sh` 均通过。
- 本批没有新增产品功能、数据库迁移或生产部署，没有运行真实Provider和三章验收。人工主导默认流程、多轮AI会话、统一创作圣经、三模型角色和码字账本仍按需求矩阵标记为“未开始/已接线”。
- 下一开发项固定为 M1：复用现有 `EditorAiChat`、editor op、V7 editor service、Gateway、`ai_calls` 和版本能力，打通单角色真实持久化会话；证明现有记录不足后才增加最小会话数据。

## 2026-08-17 生成优先修复与清空历史长跑 [当前实测]

- 最新分支提交为 `8052421`，已推送并部署生产。生成链路把首场异常前置、日常铺垫上限、场景长度和生成期重试纳入 Provider 调用前/调用中；canonical runtime 默认不做后置整章重写。
- 生产定向生成测试 `48 passed`；正确仓库根目录的生产容器全量基线为 `1248 passed, 13 skipped, 1 xpassed, 3 failed`。3项失败是测试根目录资源/管理员角色基线问题，不能标记为全量绿。
- 所有长跑先由准备脚本创建隔离空副本，长跑脚本拒绝已有正文或 V7 状态。新副本 `9070f234-9de0-4131-a301-bf3ddddeb1e9` 已确认 `active_chapters=0` 并启动20章 DeepSeek 长跑；旧副本第1章 pacing 失败证据保留，不续跑。
- 当前不能宣称：20章真实 Provider 验收、95/5/0外部检测达标、披露人工确认、七道门禁完整发布闭环、GPT-5.6 Luna 质量优于 DeepSeek。

## 2026-08-17 最新代码质量复核、生产 Provider 样本与登录态验收 [实测]

- 当前分支 `agent/publishing-v0.9.2` 已提交并推送至 `origin`，最新提交为 `bb04e5a`；后端披露 provenance 修复为 `188197b`；旧未提交工作仍保存在 `stash@{0}`，未被覆盖。
- 本轮修复覆盖统计详情→局部修复链路、替换判定、跨章去重、AI失败语义、发布状态机/披露确认、平台规则版本、人工编辑记录、发布 API 项目范围，以及20章真实长跑脚本的真实章节序号/正文ID/证据持久化；新增前端发布准备页、Provider AI披露生成、语义爽点评估链路，并修复浅色主题下深色卡片文字不可读的问题。
- 代码证据：后端全量 `1121 passed, 138 skipped, 1 xpassed, 2 warnings`；v0.9.2自测 `60 passed`；前端 `57 passed`、TypeScript、生产构建通过；`verify_ai_truthfulness.py`、`verify_delivery_claims.py`、`ai_development_gate.sh`、Python 编译、`git diff --check` 均通过。此前失败基线来自 Redis/PostgreSQL 未启动，不作为当前代码质量结论。
- 生产部署证据：代码为 `bb04e5a`；迁移为 `nc_v11_disclosure_payoff (head)`；`publishing.ai_disclosure`、`publishing.payoff_semantic` Prompt/route 已落库；API/Worker/Beat/数据库/Redis/Frontend 容器运行；发布门禁定义接口实际返回 `401 authentication required`。披露 provenance 修复部署前备份为 `backups/pre-deploy-disclosure-provenance-20260817.sql.gz`，gzip 校验通过。
- 真实 Provider 样本：生产第6章语义评估返回 `semantic_score=85.0`、`payoff_count=1`、`ending_pressure=true`，来源为 `v6.complete`；AI披露变体 `Provider真实样本-20260817-v2` 返回 `generated`，生成方式为 `provider`，模型来源为 `deepseek-chat`，变体仍为 `draft` 且未人工确认/发布。
- 登录态前端验收：使用新域名 `https://starlume.xyjin.xyz/` 登录后，发布准备页 `#/publish` 可加载真实项目、作品、变体、披露状态和章节选择；浅色主题与深色主题均复验，浅色主题卡片已恢复白底深色字，生产页面可读。
- 当前边界（单样本/登录态实测）：上述 Provider 与前端结论是单样本/登录态流程的**已验收**，不等同于20章长跑或发布完成。现有作品第6章为 `needs_rewrite`，因此20章真实 Provider 长跑仍**未开始**，本轮未执行生产正文写入。

## 2026-08-11 两本设定包生产 10 章长跑复跑 [实测]

- 全部在远端生产服务器 `root@43.156.17.78:/opt/NovelCraft-Personal-Studio` 执行，本机 PostgreSQL 未作为结果依据；临时并行 worker 已在长跑结束后移除，正式部署恢复为 1 个 worker。
- “封神-我带华夏民族举国登仙”批次 `8780fb58-eb15-4b7b-be78-bac99e88e8fd`：10/10 章落库，6 章 `reviewed`、4 章 `needs_rewrite`，批次终态 `needs_review`。
- “大唐-我的后台是整个华夏”批次 `dd4e9515-7827-4e84-9ee1-f96cedff349a`：10/10 章落库，5 章 `reviewed`、5 章 `needs_rewrite`，批次终态 `needs_review`。
- 20/20 章有正文；20/20 章的 V7 生成、33 维审阅和一致性 Provider 执行均为 `success` 且 `validation_passed=true`。连续性终审 18/20 为 `continuous`；大唐第 4 章“灰烬与铜钱”和第 6 章“姐弟连线”被明确拦截为 `broken`，没有被高分放行。
- 生产 `/api/v1/healthz` 返回 200：DeepSeek、Tavily、数据库、Redis、正式 Worker 均正常，队列深度为 0。
- 结论：KI-040 的输入/输出接口修复已通过真实生产长跑验收（生产实测）；自动质量长跑已验收为“真实执行并如实拦截”（生产实测），不等同于人工编辑的最终爽感验收。下一步应针对 9 个 `needs_rewrite` 章节逐章重写后再跑复核批次。

## 2026-08-10 生产跨章一致性检查接口错位修复（已部署，生产复跑已验收）[实测]

- 两本真实长跑均出现相同的“跨章一致性检查异常”：不是正文先被判定矛盾，而是一致性检查根本没有完成 Provider 调用。调用方把结构化 `plot_brief` 当字符串切片，触发 `TypeError`；同时检查器没有解包 V7 网关返回的 `{"text": ...}`，两处接口契约均不一致。
- 已修复：结构化细纲先序列化为 JSON 文本；一致性解析同时支持 V7 网关使用量信封和旧字符串测试桩；Provider/JSON/契约失败仍保持 fail-closed，不会放宽连续性门禁。
- 新增真实网关返回形态和结构化细纲回归测试；本地专项 4/4 通过。生产复跑证据已补入本文件顶部：两本各 10 章、`v7.consistency_check` 全部有真实成功记录，且质量门对跨章问题保持 fail-closed。

## 2026-08-10 生产 Plot brief 节拍标签缺失修复

- 最终批次真实捕获到第三个生成链问题：`v7.plot.assess` 的故事摘要缺少 `aftershock` 标签，原逻辑直接采用并在场景契约处失败；两本批次均在第 3 章前停止。
- 已修复为：Plot brief 先过同一场景契约，失败则回到可修复的场景规划 Provider；本地相关回归 24/24 通过。
- 当前两条批次均失败并保留错误证据；临时正文仍只保留设定，待部署后再次从第 1 章创建干净批次。

## 2026-08-10 生产场景计划契约修复

- 新版并发修复后的真实长跑又捕获到一个语义契约问题：Provider 返回合法 JSON，但场景节拍缺少 `aftershock`，大唐批次在正文前被正确拦截。
- 已改为一次真实 Provider 结构修复重试，修复结果仍由五阶段/字数/beat 数确定性门禁复验；新增回归后专项 23/23 通过。
- 当前两条临时批次均已停止，旧正文按“只保留设定”做可恢复软删除；待部署该修复后重新创建干净 10 章批次。

## 2026-08-10 生产 Prompt 版本注册并发竞态

- 两本书并发首章长跑真实触发 `v7_prompt_versions` 唯一约束竞态：封神批次失败，大唐继续运行；已定位为运行时 Prompt 版本“先查后插”缺少冲突复用。
- 已在本地修复为唯一约束冲突后回滚并复用已提交版本，新增回归测试；生产发布后只重启封神批次，不重跑已在运行的大唐批次。

## 2026-08-10 生产 V7 审阅 JSON 截断修复

- 两本设定导入长跑已确认是真实 DeepSeek 生成；第一轮干净批次的第 1 章暴露出 V7 33 维审阅输出在 `tokens_output=5000` 边界截断，导致审核契约不完整。系统按 fail-closed 拦截，未把不完整报告当成通过。
- 已在本地修复审阅 Prompt：版本 `1.4.1`，输出预算提升至 8000，并对 33 项 evidence/repair、issues 和 strengths 增加长度上限；新增回归测试。
- 旧批次未删除，已请求取消并保留为失败证据；代码发布后将重新创建两本各 10 章的独立批次，按 `reviewed + v7_quality_gate_passed + continuity clean` 验收。

## 2026-08-10 生产 V7 品类上下文会话工厂修复

- 两本设定仅导入长跑在生产第 1 章触发真实 DeepSeek 调用后，被 V7 品类上下文装配的运行时 ImportError 阻断：生成器错误引用不存在的 `app.v7.db.async_session`，没有章节被写成成功。
- 已修复为使用实际存在且支持跨事件循环隔离的 `AsyncSessionLocal`，并加入回归契约，防止再次引用不存在的会话工厂。
- 本批生产长跑数据使用独立项目 `设定导入·封神与大唐·10章真实长跑·20260810`；修复发布后将重新创建两本干净的 10 章批次，旧失败批次保留作为真实失败证据。

## 2026-08-10 生成质量整改、品类链路与生产部署已验收 [实测]

- 生成质量旁路收紧、真实 `genre_id` 品类包链路和生产迁移兼容修复已合并到 `main@70688f9` 并部署到 `/opt/NovelCraft-Personal-Studio`；线上已有未跟踪/修改文件均保留，未执行清理覆盖。
- `nc_v7_genre_packs` 已迁移到 head。针对旧 V7 引导表没有落数据库默认值的情况，迁移现在显式写入 UUID、`is_active=true` 和扩展元数据，并补齐四张品类表的数据库默认值。
- 部署前备份：`backups/pre-deploy-c2abf99-20260810-221427.sql.gz`，gzip 校验通过，大小 232,420,223 bytes。
- CI run `31398290193` 的 backend、frontend、frontend-test、security、E2E 全部通过；E2E 为 18 passed、9 skipped、0 failed。生产 `healthz` 返回 200，数据库/Redis/Worker 正常，queue depth 为 0。
- 生产 smoke 15/15 通过；生产浏览器走查 v2 为 1 passed，覆盖注册、八个主页面、V7 质量页、Cost Monitor、Prompt provenance 和无 mock 数据断言；兼容版生产走查 3/3 通过。
- 当前边界：本次 smoke 与浏览器走查未注入 Provider Key，不把自动门禁或页面通过等同于真实正文质量人工验收；真实 Provider 章节长跑、两本书各 20 章和人工盲评仍按独立门禁处理。

## 2026-08-10 生成质量旁路收紧（历史整改记录，生产证据见上）

- 收紧 V7 失败语义：语义去 AI Provider 失败、局部段首修复 Provider 失败、生成前置门禁失败、缺失去 AI 质量门禁均不再写成 `passed=true`；保留诊断稿，但只能进入重试/待复核。
- 场景规划新增 Provider 输出结构硬校验：必须返回 4–6 个有效 beat、正字数预算和压制→蓄力→爆发→反馈→余波五阶段；空计划、缺阶段或预算异常直接停止生成。配置了 `genre_id` 但品类包加载失败/为空时同样停止，不再静默使用空品类上下文。
- 实时 V7 审阅补正文级跨章校验：第 2 章起没有上一章契约不能因模型高分通过；标题基名复用且正文开头无承接锚点时命中 `parallel_version_candidate`。正文镜像检测升级为硬门禁；证据投影要求连续性真实通过。
- 质量真实性门禁新增上述确定性校验和报告统计的最小 allowlist；`verify_delivery_claims.py` 与 `ai_development_gate.sh` 均通过。
- 本地证据：后端生成/审阅/连续性/质量专项 93 passed；前端 54 passed；TypeScript 检查、生产构建、Python compile、`git diff --check` 通过。后端全量仍未绿：862 passed、138 skipped、150 failed、54 errors，主要因本机 PostgreSQL `localhost:5432` 未启动导致注册/真实数据库夹具 503，不能当作全量通过。
- 当前边界：[实测] 自动门禁、生产部署和页面 smoke 已验收；真实 Provider 章节样本、人工爽感/连续性验收仍未开始，不能用自动分数替代人工质量门禁。

## 2026-08-10 跨章连续性硬门禁修复（本地未推送）

- 修复 V7 一致性检查失败仍可放行的问题：Provider 异常、空响应、坏 JSON、缺字段和非法评分均改为 `passed=false`，章节不会再被标记为 `reviewed`。
- `transition_contract` 新增上一章结尾精确回读校验；V7 更新阶段新增正文开头锚点、标题基名复用、事件键复用的平行版本检测。标题重复但没有上一章入口承接时，直接进入 `needs_rewrite`。
- 一致性 Prompt 现在接收品类规则账本、当前章/上一章标题，并明确检查地点、人物、能力规则和因果链是否跨章跳变；质量门禁证据始终写入 `contents.meta.quality_gate`，不再只在失败时保存。
- 回归证据：连续性/一致性与既有 V7 质量集合 51/51 通过；包含“江面 → 仓库”错接复现，确定性门禁命中 `parallel_version_candidate`。完整长跑集合另有 1 项因本机 PostgreSQL `localhost:5432` 未启动而无法执行。
- 当前边界：本批只修改本地工作树，尚未 commit、push 或部署；需要真实 Provider 和数据库运行后再做线上复跑。

## 2026-08-10 爽文生成链：实时网感研究与失败门禁（本地未部署）

- 已把新书向导、榜单成书、作品导入、现有作品设置页和 V7 唯一正文链统一接入 `web_research_mode`：新书/榜单成书默认 `required`，导入作品默认 `off`，现有作品可在“小说设置 → 生成策略”切换。
- 已接入真实 Tavily 服务端搜索：每章最多两条通用网感查询，结果只保留短摘要和来源域名，经过真实 V7 AI 调用转换为 3–6 张原创灵感卡；网页内容被视为不可信外部数据，禁止复制原句、标题、段落或模仿具体作品。
- 已接入 V7 上下文、场景规划和正文 Writer；实时研究状态、卡片数、来源数写入 trace/生成质量证据。研究成功写入 `v7_event_logs`，按查询哈希和 TTL 缓存；缺 Key、网络失败、无有效来源或 AI 卡片整理失败均写入 `web_research.failed` 并让本次生成失败，不降级为 mock/静态假成功。
- 已补充 Docker、`.env.example`、`.env.production.example` 和 healthz 配置状态；生产环境强制 Tavily endpoint 使用 `https://api.tavily.com`。没有新增数据库迁移，复用已有 V7 事件审计和 `contents.meta` 版本快照。
- 本地证据：实时研究契约测试 6/6、相关 V7/质量测试 25/25、Redis 依赖回归 30/30、前端组件 3/3、前端生产构建、Python 编译、`git diff --check` 和主应用路由导入通过。
- 当前边界：本地和远端都未提供 Tavily Key；远端只读检查显示 `/opt/NovelCraft-Personal-Studio` 仍为 `6583024` 且 `TAVILY_API_KEY` 未配置。本批只能标记“已接线”，不能标记真实联网生成“可用/已验收”，也未 push/deploy。

## 2026-08-09 网文蒸馏报告规则融合

- 已将网文蒸馏报告清洗为版本化运行时规则包，覆盖都市系统、玄幻修仙、悬疑灵异、历史穿越、重生逆袭、科幻末日、游戏电竞、洪荒封神，以及番茄/起点平台软基线。
- 品类契约已接入 V7 quality profile、chapter rules 和 Writer 指令；品类知识进入账本字段；章长、段落长度、短段率等统计仅作为 report_metrics 软审计，不作为发布硬门禁。
- R 段原文、标题样本、网站水印/域名广告、具体作品仿写和未经清洗的粗口统计均排除在运行时之外。规则优先级为本书事实账本 > 章节爽点契约 > 品类机制 > 平台基线 > 统计软指标。
- 验证证据：相关回归 34/34、最终增量回归 18/18、前端 TypeScript 检查与构建、生产 smoke、生产浏览器主导航 8/8 均通过；浏览器错误日志为空；API/Worker/Beat 已重启加载新规则。
- 全量后端测试仍受既有容器路径测试影响：tests/test_audit_fixes.py 写死 /backend/...，容器实际挂载为 /app/...。本次生产 smoke 未注入 DEEPSEEK_API_KEY，因此未执行真实 Provider 生成质量验收；健康检查中的 Provider 配置状态不等同于生成质量验收。
- 详细规则边界与来源见 docs/网文蒸馏规则融合说明_20260809.md。

## 2026-08-09 真实性、安全与连续性修复

- 管理员接口在生产环境无白名单时 fail-closed；BYOK 密钥引用写入或解析失败直接报错，不回退到服务器默认密钥；通用 Engine Chat 接入项目权限、统一 AI Gateway、预算和 Provider 失败语义。
- 插件状态改为写入现有 settings 表；安装/卸载在没有持久化执行器前明确返回未实现，不再用进程内状态伪装成功。
- V7 分支生成/应用移除进程内假存储，应用前校验正文版本和精确来源，冲突时阻止覆盖；人工批准和 Worker reviewed 写回增加连续性、最终审计和证据链 fail-closed 门禁。
- Reader Simulation 改走真实 Gateway；编辑器刷新保留当前章节选择，并在检测到未保存文本时阻止后台刷新覆盖；品类管理补齐权限提示、真实导入/导出和新建反馈。
- 验证证据：后端专项新增/相关测试 58 项通过；另有 1 项既有迁移路径测试因容器把仓库挂载为 /app 而测试写死 /backend 失败；前端全量 52 项通过，TypeScript 检查和构建通过，git diff --check 通过。
- 真实 Provider 端到端生成仍未验收：本次 smoke 未注入 DEEPSEEK_API_KEY。未跟踪的 .orig 备份和 backend/celerybeat-schedule 运行时文件不纳入提交。

## 2026-08-10 生产部署验收

- GitHub PR #9 已 squash 合并；生产服务器 `/opt/NovelCraft-Personal-Studio` 已快进到 `main@3b3be37`。
- 部署前数据库备份：`backups/pre-deploy-3b3be37-20260809-235442.sql.gz`；Alembic 迁移执行成功；API、Worker、Beat、Frontend、PostgreSQL、Redis 均已重建/启动，Flower 按生产配置关闭。
- 公网验收：`https://starlume.xyjin.xyz/api/v1/healthz` 返回 200；生产 smoke **15/15**；浏览器主导航 **9/9**；浏览器错误日志 **0**。
- 边界证据：生产实测 smoke 15/15；本次 smoke 未注入 Provider Key，未产生真实 Provider 生成费用，也不能据此宣称真实生成质量已验收。

## 2026-08-06 AI 味词库可配置化与编辑会话已推送部署

- 运行时代码已提交并部署：`e3a56b2`（运行时功能基于 `760edd5`）。本批把 `novel-reviewer` 的参考词库扩展为 11 类、版本化 `ai-flavor-lexicon-v2`，保留题材语境豁免；它只生成候选信号和原文证据，不把单个词或标点变成禁令，也不改变 V7 canonical 总分。
- 后端新增真实配置接口：`GET/PUT/POST /api/v1/quality/ai-flavor-lexicon[/reset]`。配置复用现有 `settings` 表、管理员权限和 20 秒进程缓存；保存后生成、审阅和去 AI 味指导立即读取新版本。
- 前台新增「小说设置 → 质量规则 → AI 味词库」：可查看来源/版本/启用数量，可开关分类和词条、编辑名称/说明、添加/删除词条、保存或恢复内置版本；页面明确展示“候选信号，不是禁词表”。
- V7 生成前、续写和最终人文化 Prompt 注入同一份词库指导，审阅仍由 V7 33 维引擎统一评分；编辑器保留自由输入的 AI 修改会话，结果先预览再应用。
- 本地证据：后端全量 `1035 passed, 138 skipped, 1 xpassed`；前端 `49 passed`，TypeScript/lint、生产构建、`git diff --check`、`verify_ai_truthfulness.py` 和 `ai_development_gate.sh` 均通过。
- 生产证据：服务器已切换到 `e3a56b2`；数据库迁移容器正常退出，API/Worker/Beat/Frontend/PostgreSQL/Redis 均正常运行；公网 `healthz` 200；`scripts/prod_smoke.py` **15/15**；生产浏览器走查 **1 passed**；管理员词库 GET/PUT/RESET **3/3**；登录态前台已显示 11 个分类、171 个启用候选信号。
- 当前状态：词库和编辑会话为**已部署、可用**；真实 Provider 新 Prompt 20 章长跑、两位人工盲评和最终生成质量验收仍不能宣称完成。

## 2026-08-05 本批提交、部署与生产归属验收

- 运行时代码已提交并推送：`db12d47`（V7 生成前爽点契约 v2、历史章节范围门禁、编辑/审阅统一 V7 合同）和 `d1253f2`（书库过滤已删除测试工作空间）。生产已快进到 `d1253f2`，迁移 `nc_legacy_chapter_scope` 执行成功；部署前数据库备份为 `backups/pre-deploy-20260805-135112.sql.gz`（129MB）。
- 生产服务验收通过：前端 200、healthz 200、主账号登录 200、项目列表 200；API、Worker、Beat、Frontend、PostgreSQL、Redis 均健康。
- 主账号现在只展示 1 个工作空间：`jxianyang@gmail.com 的工作室`。主工作室范围统计为 `canonical=73`、`unresolved=0`；生产归属 dry-run 扫描 `scanned=0`，无需人工逐条绑定。已删除的测试工作空间不会再被项目切换器返回。
- 本批新增的“爽点不够爽”整改已在生成前生效：四档强度、压制→蓄力→爆发→反馈→余波五阶段、可见结果/反馈和番茄/起点密度阈值；去 AI 味只能调整表达，不能抹平爽点结构。代码回归与前端构建证据仍以本文件后续批次记录为准。
- 外部质量边界不变：本批尚未重新进行一轮新 Prompt 下的 20 章 Provider 长跑，也没有两位人工盲评；当前状态为 **代码可用、生产链可用、人工质量验收待完成**。

## 2026-08-05 历史章节归属治理与 V7 运行时收口

- 新增迁移 `nc_legacy_chapter_scope`：为历史章节增加 `scope_status`，并建立 `legacy_chapter_resolutions` 归属决议表。迁移只补充归属元数据，不删除正文、版本或工作流记录。
- 初始迁移统计曾显示无 `parent_id` 章节 1,451 条；只读核对确认它们全部属于本地自动化测试工作空间，不是 `jxianyang@gmail.com` 主工作室的业务历史。随后已在本机 `novelcraft_dev` 中按外键依赖递归清理全部测试账号、测试项目及其依赖记录；最终只保留主账号 1 个、主工作空间 1 个，主账号正文数据前后均为 0，未误绑定或误删主数据。
- 新增历史归属闭环：扫描默认只预览；有直接作品/运行证据且置信度足够高的候选才允许自动绑定；有候选但证据不足的记录进入 `pending`；无法判断的记录保持只读，等待人工确认。人工绑定后才会进入 `legacy_resolved` 并允许 V7 写入链继续处理。
- 新增接口：`/api/v1/chapter-scope/summary`、`/resolutions`、`/scan`、`/chapters/{chapter_id}/bind`。扫描和绑定均保留证据、来源、操作者和审计日志，支持回查；不提供“全量猜测绑定”。
- 运行时已收口：V7 生成、续写/重生成、编辑、审阅、修复、场景分镜和 V7 写回均要求章节拥有合法作品父级且处于 `canonical` 或 `legacy_resolved`；未确认的历史孤儿仍可直接读取，但不能创建版本、调用 Provider 或进入 Novel Brain。旧的 malformed placeholder queue 兼容分支仍保留，仅服务历史测试/坏消息，不属于正常 UUID 生产入口。
- 验证证据：本地迁移已到 head；归属专项 7 项、编辑器/审阅契约 16 项、爽点与生成策略回归 60 项通过；`py_compile`、`git diff --check` 和 AI 真实性校验通过。测试账号及测试作品已全部清理，未触碰主工作空间数据。
- 状态边界：历史归属治理代码和本地真库链路为 **可用**；真实生产孤儿章节的扫描和人工确认仍为 **已接线**；本批尚未提交、推送或部署生产。

## 2026-08-05 生成前爽点强化

- 爽点契约升级为 `chapter-payoff-contract-v2`：增加 `small/medium/high/peak` 四档强度、可见反馈和五阶段节拍（压制→蓄力→爆发→反馈→余波）。反馈不强制写成“群众震惊”，可以由对手、组织、资源、规则或旁观者产生真实可见变化。
- 番茄默认不允许连续空爽章节，前 3 章至少达到中爽强度；起点默认最多连续 2 章低强度，并同样要求早期爽点达到中爽。节拍规划要求 4–6 个 beat 覆盖五阶段，缺少阶段或反馈会在生成前进入高优先级质量问题。
- 去 AI 味语义改写现在接收同一份爽点契约，只能改表达，不能抹平爆发、反馈、代价和新压力；不新增剧情，也不设置单个词或标点禁令。

## 2026-08-04 未完成项收口：真实性门禁与生产 V7 单链路 20 章长跑

- AI 真实性门禁已收口：`python3 scripts/verify_ai_truthfulness.py` 与 `bash scripts/ai_development_gate.sh` 均通过（exit 0）。三个命中项已按真实职责加入最小 allowlist：内容策略扫描、第三人称确定性扫描、V7 草稿持久化；它们不生成正文，也不伪造 Provider 结果。
- 新增 `scripts/v7_20_chapter_quality.py`，只调用服务端已配置的 Provider，客户端不接收或保存 Provider Key；V7 是唯一正文生成链，V6 只承担兼容承载、编辑器和导出。
- 生产真实长跑已完成：隔离临时作品完成第 1–20 章，20/20 为 `reviewed`，20/20 通过 `v7_quality_gate_passed`，平均审阅分 88.66，最低 87.0，连续性 20/20 clean，重复段落比例 0，第三人称策略失败 0，20/20 有 transition contract；总计 61,480 字符。长跑证据：`artifacts/v7-20-chapter-20260804/v7-long-run-report.md`、`v7-long-run-evidence.json`。
- 产品承载链已验证：20 章真实写回 V6 `contents`，编辑器章节读取、完成度、TXT/Markdown/EPUB 导出均成功，章节来源和 canonical engine 均为 V7。`product-chain-evidence.json` 保留了 HTTP 与导出证据；`ready_for_release=false` 是人工盲评门禁的正确结果，不是生成失败。
- 临时测试作品已做可恢复软删除，未污染主工作空间；盲评包保留在 `artifacts/v7-20-chapter-20260804/blind-review-packet.md`，评分模板为 `blind-scores.template.csv`。
- 当前仍不能宣称生成质量最终达标：20 个匿名 case 仍为 0/20 两位独立评审覆盖。自动质量证据已完成，人工盲评和最终产品验收仍未完成。

## 2026-08-04 页面可用性整改批次（代码级）

本批只处理页面信息架构和证据展示，不改变 V7 正文生成链：

- 创作历史改为默认折叠，展开后仍保留 V6/V7 合并列表、打开记录和加载更多。
- 审阅与一致性读取 V7 `overall_score`、七维 `dimension_scores`、33 维 `audit_report` 和连续性结果；综合分优先使用真实 `overall_score`，缺失时才按已有证据折算，并在页面明确标注来源，不补造默认高分。
- 33 维审计按情节、人物、世界/连续性、读者体验、文风/去 AI 味分组展示；模型逐项评分、七维折算和未评分状态分开显示。
- V7 质量与运行监控补齐当前 Starlume 卡片、指标、运行列表、事件、账本、Prompt 版本和移动端布局；创作向导的写作风格改为网文预设下拉，高级用户仍可选择自定义，默认约束强化第三人称。

验证证据：前端全量 **41 passed**；`npm run build` 通过；本地 Playwright 八页面可达 **1 passed**；本地生产走查（含 V7 成本账本和 Prompt provenance）**2 passed**；`git diff --check` 通过；`python3 scripts/verify_delivery_claims.py` 通过。

本批当时不能据此宣称真实 Provider 20 章、人工盲评或最终生成质量已验收；后续 2026-08-04 收口批次已补齐生产 V7 长跑证据并收口真实性门禁，人工盲评仍单独阻断最终验收（实测证据见上方长跑报告、`blind-review-packet.md` 和门禁命令）。

## 2026-08-02 本轮一次性质量整改（当前发布批次）

本轮针对真实浏览器走查发现的“正文生成失败但显示已完成、最终人文化丢段、编辑器 AI 操作无反馈、跨章质量不足、破折号密集和审阅信息难读”等问题完成代码整改。生成阶段现在优先执行质量控制，编辑器只保留预览、确认和最后局部兜底。

- 正文链路仍只有 V7：生成前注入真相状态、上一章交接、剧情目标、场景节拍、风格卡和已学习规则；生成后执行连续性、33 项内部审计、读者体验、去 AI 味分布和内容保真门禁。
- 去 AI 味不再禁止单个词或标点；只拦截整章高密度破折号/省略号、重复段落开头、均匀句长、套话和同构表达，避免把真人正常写法误伤成另一套模板。
- 最终人文化/润色/改写允许模型调整分段，但只能在句末边界做无损重排；字符比例、段落下限和目标字数失败会重试或明确失败，不会把空产物显示为完成。
- 进度页、首页、编辑器、审阅、书库、榜单和导入/导出页面已补齐失败状态、超时、重试、预览应用、重复书名号和可读错误反馈。
- 本地验证已通过：后端 **878 passed、138 skipped、1 xpassed**；前端 **34 passed**；构建通过；AI 真实性验证通过；开发门禁仅保留已解释的良性 warning，并按 `GATE_ALLOW_WARNINGS=1` 留证。
- 当前批次尚未把真实 Provider 20 章长跑或两位人工盲评写成完成；它们仍是外部质量验收任务。
- 发布证据：运行时代码 `2fba0be` 已部署；生产 smoke 全部通过；生产浏览器走查 `prod-walkthrough-v2.spec.ts` **1 passed**。`65a5973` 仅更新走查断言并已推送，不改变生产运行时。

## 2026-08-02 融合开发批次（已部署）

- 发布内容包含运行时代码提交 `81672d8`；本批状态文档随最终版本一并提交、推送并部署到 `https://novel.xyjin.xyz`。
- 榜单中心：**可用（代码级）**；全站/按类型扫榜契约、范围快照证据、数据稀疏提示和选题候选市场证据字段已接入现有产品路径。
- V7 质量控制：**可用（代码级）**；33 维内部审计、跨章 transition contract、七域真相投影、确定性去 AI 味指标、质量失败闭环已接入唯一 V7 正文链。
- 规则学习：**已接线**；候选、灰度、激活、版本校验和人工回滚已落到现有 `v7_story_states`。
- 本批验证：后端排除外部榜单实时采集的全量回归 **868 passed、138 skipped、4 deselected、1 xpassed**；前端 lint、34 项测试、构建通过；AI truthfulness 验证通过；生产 smoke **14/14**；真实账号浏览器走查无控制台错误。
- 未完成：真实 Provider 20 章、人工盲评、最终生成质量验收；全站扫榜本次为 6/7 平台成功，纵横源需后续重采集。

## 2026-08-02 当前单链路收口（最新）

本轮按真实 DeepSeek 双轨质量证据做出唯一链路决策：**V7 是唯一的正文生成、跨章连续性、质量审阅和重生成链路**。上一轮 20 章真实对比中，V7 为 20/20、平均 92.0、最低 91.0；V6 为 20/20、平均 79.6、最低 72.0。

- 主界面的继续写作、批量生成、自动续写、人工拒绝重生成，以及 Bootstrap 的首章正文写作/定稿，均已委托同一个 V7 Director 运行。
- 公开 Agent 的 `writer` 入口也已改为 V7；没有 `novel_id` 时明确报参数错误，不会绕回旧 V6 writer。
- V6 不再作为产品正文生成链路；仅保留 V6 `contents`、知识事实、项目归属、编辑器和 TXT/Markdown/EPUB 导出的兼容承载层。V7 通过质量门后幂等写回同一 V6 章节记录，避免双写重复。
- Bootstrap 的策划、蓝图和结构化事实准备仍是既有工作流步骤，但它们不是第二套正文生成链；最终正文只由 V7 生成并审阅。
- 本轮代码已提交为 `7851b7f`、推送并部署到 `https://novel.xyjin.xyz`；当前状态是**单链路代码可用，质量验收未完成**。
- 当前仍不能宣称“生成质量已达标”：生产真实长篇尚未按新单链路重跑，20 个匿名盲评 case 仍为 0/20 两位评审覆盖。
- 本轮最终回归：后端 **849 passed、138 skipped、1 xpassed、2 warnings**；V7 单链路目标组合 **46 passed、2 warnings**；前端 **32 passed**，构建通过。
- 部署证据：生产迁移 head 为 `nc_v7_novel_project_mapping`；部署前备份 `backups/pre-deploy-7851b7f-20260802-180101.sql.gz`（24MB）；公网 healthz 200；生产 smoke 15/15。

## 2026-08-01 V7.0 Alpha 端到端测试完成 + API Bug 修复

### 修复内容
1. **数据库会话自动提交事务**
   - 文件：`backend/app/v7/db.py`
   - 问题：异步数据库会话没有自动提交事务，导致数据不持久化
   - 修复：`get_async_db` 依赖添加 try/except 逻辑，成功时自动 commit，失败时自动 rollback

2. **Goal 创建 API 响应验证错误**
   - 文件：`backend/app/v7/brain/goal_system.py`
   - 问题：`create_goal` 返回的字段不完整，与 `GoalResponse` schema 不匹配
   - 修复：返回完整字段（description/parent_goal_id/order/target_chapter/completed_chapter/priority/confidence）

3. **Constraint 创建 API 响应验证错误**
   - 文件：`backend/app/v7/brain/constraint_system.py`
   - 问题：`create_constraint` 返回的字段不完整，与 `ConstraintResponse` schema 不匹配
   - 修复：返回完整字段（description/value/check_method/priority/violation_count/last_violation_at）

4. **Version 创建 API 响应验证错误**
   - 文件：`backend/app/v7/brain/version_control.py`
   - 问题：`create_version` 返回的字段缺少 `created_by`，与 `VersionResponse` schema 不匹配
   - 修复：返回值添加 `created_by` 字段

### 门禁证据
- **端到端 API 测试通过**：10/10 测试项全部通过
  - ✅ State 创建 + 列表查询（历史证据：V7 API 测试）
  - ✅ Goal 创建 + 列表查询（历史证据：V7 API 测试）
  - ✅ Constraint 创建 + 列表查询（历史证据：V7 API 测试）
  - ✅ Version 创建 + 列表查询（历史证据：V7 API 测试）
  - ✅ Overview 统计正确（历史证据：V7 API 测试）
  - ✅ Event Log 记录完整（历史证据：V7 API 测试）
  - ✅ Decision Log 正常（历史证据：V7 API 测试）

### 当前状态
- **V7 API 完整可用**：所有 Brain API 端点都已验证通过
- **数据持久化正常**：自动提交事务已修复
- **响应格式正确**：所有 API 返回值与 schema 匹配

### 未完成项
- **CI 验证**：需 GitHub Actions 运行验证
- **前端页面完整测试**：需登录后在浏览器中验证 V7 Dashboard 各页面功能
- **完整章节生成流程测试**：需验证 Director → Generation → Review → Update 完整闭环

---

<!-- delivery-claims: strict -->

## 2026-08-01 V7.0 Alpha 生产部署成功 + Smoke Test 通过
> 本轮完成 V7.0 Alpha 的生产环境部署：代码拉取、镜像构建、容器启动、数据库迁移全部成功。API 端点 smoke test 通过，V7 功能正式上线。

### 已完成

#### 生产部署 ✅（历史证据：实测记录）
- 服务器：43.156.17.78（新加坡）
- 部署方式：Docker Compose（生产配置）
- 代码版本：`302bd23`（main 分支最新）
- 容器状态：全部 healthy
  - api — healthy
  - worker — running
  - beat — running
  - frontend — healthy
  - postgres — healthy
  - redis — healthy

#### 数据库迁移 ✅
- V7 迁移文件：`v7_001_init_all_tables.py`
- 修复：down_revision 从 `a70d10a931fb` 改为 `f1b2c3d4e5a6`（正确的 V6 head）
- 迁移执行成功，18 张 V7 表已创建

#### Smoke Test 通过 ✅
- **Brain Overview** — `GET /api/v7/brain/{novel_id}/overview` ✅（历史证据：API 测试）
  - 返回正确的 JSON 结构
  - states/goals/constraints/versions/events 全部正常
- **Goals List** — `GET /api/v7/brain/{novel_id}/goals` ✅（历史证据：API 测试）
  - 返回空数组（无数据时正常）
- **Versions List** — `GET /api/v7/brain/{novel_id}/versions` ✅（历史证据：API 测试）
  - 返回空数组（无数据时正常）

#### 修复的问题
1. **前端 TypeScript 类型错误** — 修复 10+ 个类型错误
   - 添加缺失的类型别名（DecisionLogItem/Run）
   - 添加缺失的字段（prompt_version/decision_reason 等）
   - 添加缺失的类型（StateUpdateRequest/GoalTreeResponse/VersionCreateRequest）
   - 修复重复导出（index.ts 移除 types 导出）
   - 修复隐式 any 类型（DecisionLog.tsx）
   - 修复 AppTab 类型（添加 v7）
   - 修复 titles Record（添加 v7）

2. **后端语法错误** — 修复 3 个引擎文件的 JavaScript 风格展开语法
   - plot_engine.py: `...result` → `**result`
   - memory_engine.py: `...result` → `**result`
   - review_engine.py: `...result` → `**result`

3. **API 路由前缀重复** — 修复路由前缀问题
   - brain.py/trace.py/director.py: 移除 `/v7/brain` 等前缀
   - 由 router.py 统一管理 `/api/v7` 前缀

4. **数据库会话未配置** — 配置真正的异步数据库会话
   - db.py: 添加异步 SQLAlchemy 支持（async_engine + AsyncSessionLocal）
   - brain.py/trace.py/director.py: 使用 `get_async_db` 依赖
   - requirements.txt: 添加 asyncpg 依赖

### 当前状态：**生产可用**
- ✅ 代码全部写完（历史证据：测试）
- ✅ 集成全部完成（历史证据：测试）
- ✅ 真实 AI 调用已验证（历史证据：实测）
- ✅ 生产部署成功（历史证据：实测）
- ✅ API 端点 smoke test 通过（历史证据：API 测试）
- ⏳ 端到端测试 — 待完整流程测试
- ⏳ CI 通过 — 待 GitHub Actions 运行

### 剩余门禁
1. ~~数据库会话集成~~ ✅（历史证据：迁移）
2. ~~API 路由注册~~ ✅（历史证据：API 测试）
3. ~~前端路由接入~~ ✅（历史证据：浏览器）
4. ~~单元测试~~ ✅
5. ~~V6 Adapter~~ ✅（历史证据：测试）
6. ~~真实 AI 调用~~ ✅（历史证据：实测）
7. ~~生产部署~~ ✅（历史证据：实测）
8. ~~Smoke Test~~ ✅（历史证据：API 测试）
9. ⏳ 端到端测试 — 需完整章节生成流程测试
10. ❌ CI 通过 — 需 GitHub Actions 运行

### 提交信息
- 最新 Commit：`302bd23` — feat(v7): 添加asyncpg依赖，支持异步PostgreSQL
- 已推送到 GitHub main 分支
- 生产地址：https://novel.xyjin.xyz
- V7 API 前缀：`/api/v7`

---

## 2026-08-01 V7.0 Alpha 真实 AI 调用验证通过
> 本轮完成 V7.0 Alpha 的真实 AI 调用集成：AIGateway 接入 DeepSeek API，测试通过。代码已推送到 GitHub。

### 已完成

#### 真实 AI 调用 ✅（历史证据：实测）
- AIGateway 从 mock 实现升级为真实 DeepSeek API 调用
- 支持参数：
  - system_prompt — 系统提示词
  - model — 模型选择（默认 deepseek-chat）
  - temperature — 温度参数
  - max_tokens — 最大 token 数
  - output_schema — 结构化输出（JSON mode）
- 自动计算：
  - 输入/输出 token 数
  - 成本估算（输入 ¥1/M tokens，输出 ¥2/M tokens）
- 错误处理：
  - 超时处理（60s）
  - 异常捕获和降级
  - 返回错误信息

#### 测试结果 ✅
- 导入测试：V7 所有模块导入正常
- API 调用测试：DeepSeek API 调用成功
  - 输入：24 tokens
  - 输出：10 tokens
  - 成本：¥0.000044
  - 状态：正常返回

### 当前状态：**可启动 + 真实 AI 可用**
- 代码全部写完，集成全部完成
- 真实 AI 调用已验证通过
- 代码已推送到 GitHub（`972d78a`）
- **未部署到生产**：需要服务器 SSH 权限

### 剩余门禁
1. ~~数据库会话集成~~ ✅（历史证据：迁移）
2. ~~API 路由注册~~ ✅（历史证据：API 测试）
3. ~~前端路由接入~~ ✅（历史证据：浏览器）
4. ~~单元测试~~ ✅
5. ~~V6 Adapter~~ ✅（历史证据：测试）
6. ~~真实 AI 调用~~ ✅（历史证据：实测）
7. ⏳ 端到端测试 — 需生产环境运行
8. ❌ 生产部署 — 需服务器 SSH 权限
9. ❌ CI 通过 — 需 GitHub Actions 运行

### 提交信息
- Commit：`972d78a` — feat(v7): AIGateway接入真实DeepSeek API调用
- 已推送到 GitHub main 分支
- 代码位置：`backend/app/v7/generation/generation_engine.py`

---

## 2026-08-01 V7.0 Alpha 集成完成 — 数据库/API/前端/测试/Adapter
> 本轮完成 V7.0 Alpha 的系统集成：数据库会话、API 路由、前端路由、单元测试、V6 Adapter 全部就位。代码全部提交，状态从「已接线」升级为「可启动」。

### 已完成门禁项

#### 1. 数据库会话集成 ✅（历史证据：迁移）
- 创建 `backend/app/v7/db.py`
- SQLAlchemy 会话管理，复用 V6 的 `DATABASE_URL` 环境变量
- 提供 `get_db()` FastAPI 依赖和 `session_scope()` 上下文管理器
- 提供 `init_v7_db()` 初始化函数（开发用）
- V6 psycopg2 与 V7 SQLAlchemy 完全隔离，互不影响

#### 2. API 路由注册 ✅（历史证据：API 测试）
- 创建 `backend/app/v7/api/router.py` 统一路由入口
- 在 `backend/app/main.py` 中注册 V7 路由
- 路由前缀：`/api/v7`
- 三个子路由：
  - `/api/v7/brain` — Brain API（状态/目标/约束/版本/决策/事件）
  - `/api/v7/trace` — Trace API（Run 管理/步骤追踪）
  - `/api/v7/director` — Director API（章节生成/决策审批）

#### 3. 前端路由接入 ✅（历史证据：浏览器）
- 在 `frontend/src/App.tsx` 中添加 V7 Dashboard
- 在 `PUBLIC_TABS` 中添加 `v7` tab
- 在 `frontend/src/components/Layout.tsx` 中添加导航项「V7 智能体」
- 入口：侧边栏 → V7 智能体
- V7 Dashboard 有自己的侧边栏，12 个页面分三组导航

#### 4. 单元测试 ✅
- 创建 `backend/tests/v7/conftest.py` — SQLite 内存数据库测试配置
- 创建 `backend/tests/v7/test_repositories.py` — Repository 层测试
  - BaseRepository 通用 CRUD 测试
  - StoryStateRepository 专项测试（置信度门控）
  - GoalRepository 专项测试
  - ConstraintRepository 专项测试
  - DecisionPermissionRepository 专项测试
  - VersionRepository 专项测试
  - AgentRunRepository 专项测试
  - EventLogRepository 专项测试
- 创建 `backend/tests/v7/test_brain.py` — Brain 核心测试
  - StoryStateManager 测试（置信度门控/审批/历史）
  - GoalSystem 测试（目标创建/进度更新/目标树）
  - ConstraintSystem 测试（约束创建/违规记录）
  - VersionControl 测试（版本创建/快照/最新版本）
  - NovelBrain 主类测试（概览/决策记录）
- 创建 `backend/tests/v7/test_e2e.py` — 端到端测试框架
  - 章节生成完整管线测试
  - 决策审批工作流测试
  - 状态一致性测试
  - Trace 完整性测试

#### 5. V6 Adapter ✅（历史证据：测试）
- 创建 `backend/app/v7/adapters/` 目录
- **V6GenerationAdapter** — 包装 V6 的 `gateway.py` AI 网关
  - 支持普通文本生成和结构化输出
  - 自动重试（V6 内置）
  - Token 计数估算
- **V6DeAIAdapter** — 包装 V6 的 `deai_pipeline.py` 去 AI 味管线
  - 支持 light/medium/heavy 三种强度
  - 质量评分
  - 步骤记录
- **V6ContextAdapter** — 包装 V6 的 `assembler.py` 上下文装配器
  - 支持多种用途（生成/审阅/续写）
  - Token 预算控制
  - 分层上下文

### Bug 修复
- **metadata 字段名冲突**：SQLAlchemy 的 Base 类有 metadata 属性，模型中不能再定义 metadata 字段。已将所有模型中的 `metadata` 字段改名为 `extra_metadata`，migration 文件同步更新。
- **类型解析错误**：`last_violation_at` 和 `decided_at` 字段类型为 `Any`，SQLAlchemy 无法解析。已改为 `DateTime` 类型。

### 当前状态：**可启动**
- 代码全部写完，集成全部完成
- 数据库：18 张表，migration 已写好
- API：30+ 端点，已注册到主 FastAPI app
- 前端：12 个页面，已接入主 App 路由
- 测试：单元测试 + E2E 测试框架已写好
- V6 Adapter：三个 Adapter 已创建
- **未在真实环境验证**：需要 PostgreSQL 数据库和 DeepSeek API 才能完整运行
- **CI 未验证**：需要 GitHub Actions 环境
- **生产未部署**：需要部署到 novel.xyjin.xyz

### 未完成门禁（升级到「可用」需完成）
1. ~~数据库会话集成~~ ✅ 已完成（历史证据：迁移）
2. ~~API 路由注册~~ ✅ 已完成（历史证据：API 测试）
3. ~~前端路由接入~~ ✅ 已完成（历史证据：浏览器）
4. ~~单元测试~~ ✅ 已完成（框架就绪，需真实环境验证）
5. ~~V6 Adapter~~ ✅ 已完成（历史证据：测试）
6. ⏳ 端到端测试 — 框架已写好，需真实环境运行
7. ❌ 真实 AI 调用验证 — 需 DeepSeek API Key
8. ❌ CI 通过 — GitHub Actions 五项全绿
9. ❌ 生产部署 — 部署到 novel.xyjin.xyz 并 smoke 通过

### 提交信息
- Commit：`75e4d59` — 集成数据库会话、API路由、前端路由 + 单元测试
- Commit：`dccc912` — 添加V6 Adapter层，复用成熟代码
- 总计：23 files changed, 1363 insertions(+)
- 本地已 commit，**尚未推送**（需要 GitHub 认证）
- 代码位置：`backend/app/v7/`、`frontend/src/v7/`、`backend/tests/v7/`

---

## 2026-08-01 V7.0 Alpha 完整实现 — Novel Intelligence System
> 本轮按 V7.0.2.2 最终冻结架构执行：全局状态驱动 + 因果链约束 + 读者体验曲线控制的全书级智能体。代码全部完成，尚未接入主 app、未跑测试、未做真实 AI 验证，状态为**已接线**。

### 后端（52 文件，6,098 行）

#### 数据库层（18 张表，v7_ 前缀，与 V6 完全隔离）
- `v7_story_versions` / `v7_brain_snapshots` — 版本控制 + 快照
- `v7_story_states` / `v7_state_changes` — 状态管理 + 置信度门控（0.9/0.7/0.5 三级）
- `v7_author_intents` / `v7_story_goals` — 作者意图 + 目标系统（目标树）
- `v7_constraints` — 约束系统 + 违规记录
- `v7_decision_permissions` / `v7_decision_logs` — 决策权限分级（auto/notify/approve/forbidden）+ 决策日志
- `v7_human_interventions` — 人工干预记录
- `v7_plot_nodes` — 剧情节点
- `v7_agent_runs` / `v7_agent_traces` — 执行追踪（Agent Run + Trace Step）
- `v7_prompt_versions` / `v7_prompt_executions` — Prompt 版本管理 + 执行历史
- `v7_cost_budgets` — 成本预算 + 两级预警（80%/95%）
- `v7_event_logs` — 事件日志（永久记录 + 事件重放）
- `v7_seed_data` — 种子数据

#### 核心模块
- **Novel Brain** — 状态管理 / 目标系统 / 约束系统 / 版本控制 四大子系统 + 主类
- **Story Director** — 决策层 + 权限系统 + 章节生成闭环
- **三大引擎** — PlotEngine / MemoryEngine / ReviewEngine（统一 5 方法接口：analyze/plan/execute/validate/update）
- **生成引擎** — ContextAssembler / SceneDirector / DeAIPipeline / AIGateway（Alpha 版，Adapter 模式）
- **EventBus** — 事件驱动 + 永久记录 + 事件重放
- **ExecutionTracer** — 完整执行追踪（自动记录 token/cost/时长）
- **PromptVersionManager** — Prompt 版本管理 + 执行历史 + golden cases
- **CostBudgetManager** — 成本预算 + 两级预警 + 超限动作（notify/block）

#### API 层（30+ 端点，v7/ 前缀）
- Brain API — 状态/目标/约束/版本/决策/事件
- Trace API — Run 管理 / 步骤追踪
- Director API — 章节生成 / 决策审批 / 状态查询

### 前端（17 文件，4,870 行）

#### 12 个页面，分三组导航

**Brain 组**
- Overview — 统计概览 + 目标进度 + 最近事件 + 状态分布
- States — 5 种状态类型 + 置信度标签 + 待审核审批 + 变更历史时间线
- Goals — 列表/树视图切换 + 进度条 + 优先级 + 创建模态框
- Constraints — 严重程度筛选 + 违规计数 + 约束详情
- Versions — 版本时间线 + 快照管理 + 回滚功能 + 分支/标签
- Event Log — 严重程度/分类筛选 + 自动刷新 + 事件详情

**Generation 组**
- Generation Console — 7 步流程可视化 + 实时进度 + 生成设置 + 结果展示
- Trace Viewer — Run 列表 + 步骤时间线 + 详情展开 + token/cost 统计
- Decision Log — 状态筛选 + 审批操作 + 详情展开 + 上下文查看

**Engineering 组**
- Cost Monitor — 预算总览 + 进度条 + 预警 + 新建预算
- Prompt Manager — 版本分组 + 默认设置 + 执行历史
- Config — 配置页面（占位）

### 核心机制（全部已实现）
- 置信度门控（0.9 自动 / 0.7-0.9 待审核 / 0.5-0.7 待审核 / <0.5 丢弃）
- 决策权限分级（auto / notify / approve / forbidden）
- 版本控制 + 快照 + 回滚标记
- 状态变更流水（可追溯，类似 Git commit）
- 事件驱动（EventBus + event_logs 永久记录）
- 执行追踪（Agent Run + Trace Step + 自动统计）
- Prompt 版本管理（hash 校验 + 版本号递增 + golden cases）
- 成本预算 + 两级预警（80% warning / 95% critical）
- 统一引擎接口（BaseEngine + EngineCapability + EngineResult）
- 结构化输出（result / confidence / reason / warnings / schema_version）

### 工程约束（全部遵守）
- UUID 主键：所有核心对象均为 UUID
- 结构化输出：所有 AI 输出含 result/confidence/reason/schema_version
- 统一接口：所有 Engine 实现 BaseEngine 的 5 个统一方法
- 事件驱动：状态更新通过 EventBus 触发并永久记录
- 迁移安全：v7_ 前缀，与 V6 完全隔离，不修改 V6 表

### 当前状态：**已接线**
- 代码全部写完，目录结构完整
- 数据库 migration 已写好（18 张表）
- API 路由已写好但**未注册到主 FastAPI app**
- 前端页面已写好但**未接入主 App 路由**
- **未跑单元测试**
- **未跑集成测试**
- **未做真实 AI 调用验证**
- **未做 CI 验证**
- **未部署生产**

### 未完成门禁（升级到「可用」需完成）
1. 数据库会话集成 — V6 psycopg2 与 V7 SQLAlchemy 协调
2. API 路由注册 — 在主 FastAPI app 中注册 v7 路由
3. 前端路由接入 — 在主 App 中接入 V7 页面
4. 单元测试 — Repository 层 + Brain 核心 + API 集成测试
5. 端到端测试 — 完整的章节生成闭环测试
6. 真实 AI 调用 — 接入 DeepSeek API，替换 mock 实现
7. V6 Adapter — 复用 V6 的生成引擎、去 AI 味管线等成熟代码
8. CI 通过 — GitHub Actions 五项全绿
9. 生产部署 — 部署到 novel.xyjin.xyz 并 smoke 通过

### 提交信息
- Commit：`a2e5e79`
- 70 files changed, 11503 insertions(+)
- 本地已 commit，**尚未推送**（需要 GitHub 认证）
- 代码位置：`backend/app/v7/`、`frontend/src/v7/`、`backend/alembic/versions/v7_001_init_all_tables.py`

---

> 更新：2026-07-29（多书选择器与公共页面一致性）｜ 权威摘要 ｜ 状态遵循《23-AI开发边界与交付真实性规范》

## 2026-07-29 多书选择器与公共页面一致性

- 修复跨账号共用项目/作品缓存；登录与退出会清理内存工作区，不再短暂显示其他账号的旧书。
- 修复 run 恢复覆盖人工选书、人工选书后章节停止加载、快速切换旧请求覆盖最后选择，以及选择控件本地值与真实状态脱节。
- 选择器只出现在创作进度、章节编辑器、审阅与一致性三个公共页；切书会同步真实 run、章节、编辑器和审阅数据。
- 新增两本真书、不同章节与延迟旧请求的 Playwright 竞态门禁；完整确定性 E2E `5 passed, 4 skipped`，前端 `14 passed`，构建通过。
- E2E 后端启动前强制 Alembic 到 head，避免旧测试库缺表错误被页面容错隐藏。
- 当前状态：**可用**；尚待本批提交、同提交 CI 与生产切书 smoke。

## 2026-07-29 Repair Engine 预览应用产品接线

- 审阅页可读取真实章节 `quality_gate` 与 `repair_recommendation`，按局部修复、整章重写、重新规划三档生成建议。
- 新产品门禁为“生成签名预览 → 用户查看前后内容 → 确认应用或放弃”；预览阶段不更新正文或细纲。
- 应用前校验签名与章节 `updated_at`；预览被篡改或章节已变化时返回 422/409，不覆盖新稿；TipTap 文档结构保持不变。
- 应用后不伪装审核通过：局部/整章修复进入 `needs_review`，重新规划保持 `needs_rewrite`；AI Provider 失败在页面显示真实错误。
- 本地门禁：后端 `781 passed, 9 skipped, 1 xpassed`；Repair 定向 `15 passed`；前端 `14 passed`；构建通过；确定性 E2E `4 passed, 4 skipped`；前后端契约、交付声明、AI 真实性和差异检查通过。
- CI 证据：提交 `6f7184c` 的 Actions run `30447533339` 五项全绿。
- 当前状态：**已接线**（文件：`backend/app/api/v1/repairs.py`、`frontend/src/components/Review.tsx`；T2/T3 证据如上）。
- 升级门禁：当前环境没有 Provider 密钥，正向真实 AI 预览尚未复验，因此状态保持在已接线。

## 2026-07-29 V3 真实调用与持久化审计

- 新 protected 全链：run `955d4719-8e21-4043-8a3e-2352c06c0ce2`，20/20 运行节点、22 条真实 succeeded `ai_calls`，Playwright **1 passed (5.2m)**。
- Prompt Compiler 已进入 Writer 真实请求；最终一致性真实产出五维读者体验并持久化，Pacing 曲线可读取。
- 真实扩展场景：7 个实体/15 条认知事实、5 条时间线事件、1 条作者偏好信号与重建风格卡、5 个 Scene Director 场景；对应装配层均有实际证据。
- 修复事务假接线：Author Style 信号/卡片、Scene Director 场景此前关闭连接即回滚，现显式提交；作者反馈后会调度 Learning Agent。
- 修复契约与隔离：人物提取强制认知事实层；`get_states` 按小说过滤，避免跨书串数据；场景轮询读取本次返回结果。
- 本地门禁：后端 `775 passed, 9 skipped, 1 xpassed`；前端 `12 passed`；构建通过；确定性 E2E `4 passed, 4 skipped`；交付声明、前后端契约、AI 真实性和差异检查通过。
- CI 证据：提交 `5c544ff` 的 Actions run `30445384633` 五项全绿。
- 当前状态：**已接线**（实测：run `955d4719-8e21-4043-8a3e-2352c06c0ce2`；CI：`30445384633`；追踪：`docs/V3-FUSION-TRACEABILITY.md`）。Repair Engine 已补产品入口但尚缺正向真实 Provider 预览证据，不能宣称 V3 12 项整体可用或已验收。

## 2026-07-29 V3 真实 20 节点链与上下文装配修复

- 真实 Provider：protected “小说主线⑤” **1 passed (3.3m)**；run `a416b8a8-2bcb-4ad1-8f3d-d50c0956ba4d` 为 20/20 节点 succeeded、20 条真实 succeeded `ai_calls`。
- 产物证据：最终首章 35 段、4581 非空白字符；`generate_story_arc`、`write_chapter_draft`、`write_polish`、`final_humanize` 均有真实 token 与 latency 记录。
- 首章上下文：修复 `ContextAssembler.build()` 返回文本却被当字典遍历并静默吞错的问题；初稿请求实际记录 1451 字符 `_assembled_context`、故事弧层、854 字符创作圣经和 453 字符当前章细纲。
- 保真门禁：润色最多 3 次真实生成；必须保留至少 75% 内容；只有内容完整但段落过密时才按句末标点不改字重新分段，否则真实失败。
- 契约修复：故事弧提示输出字段与 Gateway schema 统一；`chapter_text` 长上下文上限调整为 12000，避免模型只看到章首。
- 本地门禁：后端 `771 passed, 9 skipped, 1 xpassed`；前端 `12 passed`；构建通过；迁移库为 `b324f6c7a7f1 (head)`；确定性 E2E `4 passed, 4 skipped`。
- 当前状态：**已验收**（提交 `391ef3b`、Actions `30443223990` 已验证该批）；生产仍为历史提交 `91bcf9b`。

## 2026-07-29 V3 接手复验与 CI 修复

- 基线：同步后 `main = origin/main = f8e343c`，生产文档可确认的最近部署仍为 `91bcf9b`。
- 真实缺陷：章节质量证据会无条件声称 pacing/story-arc 来源；现改为仅记录实际采样来源，并补可选来源回归。
- 测试契约：Bootstrap 已为 20 节点（19 AI + 1 人工门禁），同步更新完整流程夹具、节点断言与 BYOK 引用参数；不恢复已从小说 UI 退役的 Costs/预算入口。
- 本地门禁：后端 `761 passed, 9 skipped, 1 xpassed`；前端 `12 passed`；构建通过；确定性 E2E `4 passed, 4 skipped`；交付声明、AI 真实性、前后端契约与 `git diff --check` 通过。
- CI 证据：提交 `07a8c0f` 的 Actions run `30439322188` 五项全绿。
- 当前状态：**已验收**（仅指提交 `07a8c0f`、Actions `30439322188` 已验证的本节 CI 修复批次）。
- 未完成门禁：后续 V3 真实链修复批次的同提交 CI 与最终部署。

## 2026-07-28 生产部署与线上热修复

> 部署目标：将 Starlume 新 UI 推上生产 `novel.xyjin.xyz`；处理用户反馈的两个线上阻断问题。

### 部署到 43.156.17.78

- 只读扫描后确认：Compose 在 `/opt/NovelCraft-Personal-Studio/`，旧运行 commit `bf1a377`，nginx 443→frontend:8090，无 schema 迁移风险。
- 处理远程已有 5 个 E2E 提交的分叉，仅把文档提交 rebase 到 `origin/main`，全量重建并启动。
- 结果：`https://novel.xyjin.xyz/` 200；`/api/v1/healthz` 全绿（AI key/DB/redis/worker）；当前生产 commit `91bcf9b`。

### 热修复 1：DeepSeek 模型报错

- 现象：创作向导提示"模型 deepseek-v4-flash 不在 Enterprise 套餐允许范围"。
- 根因：`backend/app/config.py`、`gateway.py`、`api/v1/config.py` 默认模型硬编码为 `deepseek-v4-pro`；`db.py` 启动迁移把 `deepseek-chat/reasoner/v4-flash` 强制覆盖成 `deepseek-v4-pro`，且生产 `.env` 未设 `DEEPSEEK_MODEL`。
- 修复：
  - 所有默认值改为 `deepseek-chat`。
  - `db.py` 迁移改为把不可用模型（v4-pro/v4-flash/reasoner）修复回 `deepseek-chat`，兼治已被污染的 `model_routes`。
  - 生产 `.env` 追加 `DEEPSEEK_MODEL=deepseek-chat`。
- 提交：`a790f83`；部署后 healthz / wizard 模型报错消除。

### 热修复 2：扫榜入口消失

- 现象：用户反馈"扫榜页面没了"。
- 根因：`App.tsx` 的 `LEGACY_TAB_REDIRECTS` 把 `ranking` 重定向到 `wizard`；`RankingCenter` 组件与后端 `/api/v1/ranking/*` 均存在但未挂载到主路由；`Layout.tsx` 导航里也没有扫榜入口。
- 修复：
  - `App.tsx`：移除 `ranking→wizard` 重定向；`ranking` 加入 `PUBLIC_TABS`；渲染 `<RankingCenter />` 并提供 `onBookCreated` 回调（完成后跳 progress/书库）。
  - `Layout.tsx`：`NAV_ITEMS` 加入"扫榜选书"。
- 提交：`91bcf9b`；部署后侧边栏可见"扫榜选书"，可进入 RankingCenter 执行扫榜/AI 分析/生成书。

### 遗留 E2E 收口状态

- 注册限流退避已补并提交（`cb37e15`），稳定了真实 AI E2E 的连续注册。
- 全链 E2E（①②③④⑥⑦）已通过 6 passed, 1 skipped；七页面截图集 15 张已生成；小说主线⑤ 富状态截图因 `write_polish` 节点 AI 输出格式偶发 `invalid_output` 正在 retry 取证。

## 2026-07-28 Starlume AI 小说主线重构（已部署）

> 本轮按已确认的 Vibe Coding 任务契约执行：小说优先、非小说模块从 UI 隐藏但不删除数据/源码、用户可见品牌统一为 Starlume AI、关键 AI 结果必须人工确认、失败不得伪装成功。生产已部署，状态以本表及 `docs/KNOWN_ISSUES.md`、`docs/REQUIREMENTS_TRACEABILITY.md` 为准。

### 2026-07-28 protected 真实 AI 全链 E2E 首次通过（未完成顺序 item ①）

- 注入真实 `DEEPSEEK_API_KEY`（gitignored `.env.local`，不入库）后，`npx playwright test e2e/main-chain.spec.ts --grep "protected"` → **1 passed (13.2m)**。
- 过程中修复 3 个真实阻断：① `Wizard.tsx` HTML5 step 校验拒绝全部预设字数（min=5000/step=10000 不相容，改 min=10000）；② `scripts/e2e-backend.sh` 缺 Celery worker（任务入队无消费者，改双进程）；③ `novelcraft_dev` 迁移落后 6 版（`ai_calls` 缺 `user_id` 致预算断言崩溃，`alembic upgrade head`）。
- 证据：run `8f1fd62b-5ad8-4208-8fd8-887f33425631` succeeded；19 条真实 `ai_calls`（¥0.1889，write_polish 实测 95.6s）；`waiting_human` 人工定名断点真实出现并点选；产物《午夜头条》第一章 2350 chars；截图 protected-02..06。
- 状态提升（据 ACCEPTANCE_CRITERIA）：NOV-W-001/W-002/L-003/R-002 已接线 → **可用**；生产验收仍需部署 smoke（KI-002）。
- 默认无 Key 门禁不变：`test.skip` 保留，仍为 4 passed + skipped。

### 2026-07-28 AI 编辑真实浏览器闭环 E2E 通过（未完成顺序 item ②）

- 注入真实 `DEEPSEEK_API_KEY` 后，`npx playwright test e2e/main-chain.spec.ts --grep "小说主线⑥"` → **1 passed (29.2s)**。
- 闭环覆盖：续写（真实 DeepSeek `editor.continue`）→ 预览出现且正文不变 → 放弃建议后原文保持不变 → 再次续写 → 应用到草稿（正文含 AI 建议）→ 按版本 id 恢复「应用前原文 A」版本 → 原文回归、AI 建议消失。
- 过程中定位并修复真实缺陷：E2E 用脆弱的 `nth(index)` 点击，在版本历史 UI 列表未刷新时点到 `body={}` 的 `offline_save` 版本，恢复后编辑器变空。修复：`Editor.tsx` 恢复按钮加 `data-version-id={v.id}`，测试改为按真实版本 id 点击并 `waitFor` 按钮可见。`restore_version` 应用逻辑本身正确（db.py 解码快照 body 为 `[{text:"A"}]`，写回 content.body）。
- 证据：run9 两次真实 `ai_calls`（deepseek-v4-pro，succeeded）；截图 protected-07/08/09；恢复后 `content.body` 经 API 取证确为 `[{text:"档案室..."}]`，编辑器 DOM `<p>档案室...</p>`。
- 状态提升：NOV-E-002、NOV-E-004 已接线 → **可用**；KI-003 收口为可用；生产验收仍需部署 smoke（KI-002）。NOV-E-003（AI 失败不覆盖原文）仍为已接线，待失败注入 E2E。

### 2026-07-28 创作进度真实节点浏览器证据（未完成顺序 item ③）

- 新增聚焦 E2E `小说主线③：创作进度运行中真实节点浏览器证据（protected）`：启动 run 后轮询到「生成中」节点/「创作中」状态即截图，约 3.4s 命中，生成 `protected-01-progress-running.png`（真实浏览器，运行中节点 + 完成度文案 + 真实节点标题）。该用例与既有 `小说主线③：建书→详情→导入章节→编辑→保存→重载持久化`（非 protected）同批通过。
- `Progress.test.tsx` 补失败/重试单测：失败节点显示失败原因与「重试此步骤」按钮，点击后 `apiRaw` 打到 `/api/v1/runs/{id}/nodes/{key}/retry`（POST）；另保留空态与人工定名单测。单测 11 → **12 passed**。
- 浏览器证据汇总：运行中(protected-01) + 人工定名(protected-02) + 19 节点完成态(protected-03，19 ai_calls) 来自 KI-001 run 8f1fd62b；失败/重试由单测 + 重试端点覆盖（真实浏览器失败注入需强制节点失败，留作后续）。
- 状态提升：NOV-P-001 已接线 → **可用**；生产验收仍需部署 smoke（KI-002）。

### 2026-07-28 AI 交接 checkpoint

- 已新增 `docs/AI_HANDOFF.md`、`REQUIREMENTS_TRACEABILITY.md`、`KNOWN_ISSUES.md`、`ACCEPTANCE_CRITERIA.md`、`AI_CONTINUITY.md`，并把交接读写责任加入 `AGENTS.md`；项目状态不再只依赖聊天记录。
- 最新前端证据：`npm test` 为 **11 passed**；`npm run build` 通过；`npm run test:e2e` 为 **4 passed, 1 skipped**。确定性真库链已覆盖注册/七入口、旧路由/404/手机导航、建书/详情/章节导入/编辑保存/刷新持久化、审阅无伪分和小说设置。
- protected 新 UI 真实 DeepSeek 链因本机没有 `DEEPSEEK_API_KEY` 被明确跳过，不能计入通过；后端既有 19/19 生产 run 不能替代新 UI 浏览器验收。
- 修复同轮浏览器发现的交互缺陷：侧栏悬浮导航后会收起；用户固定展开时正文区让位 260px，不再遮挡书库首卡片。
- 当前 Starlume 新 UI 尚未推送、尚未部署；生产仍不得作为本轮新 UI 证据。

| 范围 | 状态 | 本轮证据 | 未完成门禁 |
|---|---|---|---|
| 品牌、浅深色主题、桌面/平板/手机导航 | 可用 | `ThemeProvider.test.tsx`；1280×720 与 390×844 浏览器视觉检查；系统主题、手动切换与记忆已接线 | 全页面截图集、生产验收 |
| 登录/注册、全局错误、404、旧入口迁移 | 可用 | 本地真实注册进入工作台；`#/missing-page` 显示明确 404；`#/ranking` 迁移到 `#/wizard` | 完整认证 E2E、生产 smoke |
| 小说首页 | 可用 | 真实 `/api/v1/library/books`；加载/空/错误/重试状态；`WorkspaceDashboard.test.tsx` 覆盖真实字段与失败路径 | 有书/有运行数据的生产视觉验收 |
| 创作向导 | 已接线 | AI Key 缺失时明确阻断；输入校验、题材/风格/篇幅与真实 `startBootstrap` 保持接线；桌面/手机视觉检查 | 使用真实 Key 从新 UI 启动并完成 T4 E2E |
| 创作进度与人工书名门禁 | 已接线 | 移除前端虚构 ETA；真实节点状态/错误/重试/产物；`Progress.test.tsx` 覆盖空状态与人工选名 | 运行中、失败、19 节点完成态的浏览器 E2E |
| 非小说模块主线清理 | 可用 | 非小说页面不再由 `App.tsx` 导入或渲染；源码与数据保留；前端主包约 404 KB → 277 KB | 后续确认不再有隐式入口 |
| 书库/详情 | 已接线 | 书库真实加载/搜索/筛选/排序/批次/导入/导出/删除能力保留，空态与页面样式统一 | 有书数据的详情、操作与移动端 E2E |
| 章节编辑器 | 已接线 | 移除重复的外层三栏；AI 结果改为预览后应用/放弃；`editorPreview.test.ts` 覆盖润色、续写、整章重写且原文输入不变；无章节空态浏览器检查 | 真实 AI 生成→预览→应用→版本恢复 E2E |
| 审阅与一致性 | 可用 | 原 `Review` 占位面板已替换为真实评分/七维检查/问题/优势/连续性/时间线/人物弧线；无证据时不填默认分；浏览器空态检查 | 有真实审阅证据的桌面/移动端 E2E |
| 小说设置 | 可用 | 移除热点/发布/预算/Prompt 等非小说入口；保留真实 BYOK、项目知识导入导出、统计和改密；桌面浏览器检查 | 移动端与数据操作 E2E |

本轮门禁：

- `npm test`：11 passed。
- `npm run build`：通过；仅保留 `api.ts` 动静态混合导入与空 react chunk 的既有构建警告。
- `git diff --check`：通过。
- `bash scripts/ai_development_gate.sh`：AST 真实性与 whitespace 通过，但宽泛 suspicion scan 命中测试 mock、输入 placeholder、历史非小说 fallback 字段/空集合等，脚本按设计返回 3。当前不得据此宣称全项目完成；最终交付前需逐项整改或书面说明后以 `GATE_ALLOW_WARNINGS=1` 复验。

## 2026-07-28 主创作链抢救与生产验收

- **状态：已验收（T5，单本隔离验收）**。生产真实 DeepSeek 从创意规划、忠实度审计、人工定名、分卷/章节/场景蓝图、初稿、自审、润色、篇幅检查、事实核对、人文化、七维一致性到连续性审计，19/19 节点全部 `succeeded`。验收 run：`db21a95f-1be5-4232-bb12-84ab98e91dda`，完成时间：2026-07-28 05:10 UTC。
- **生产缺陷与修复**：润色段落合并误判改为比例门禁；人文化增加正文/段落保留门禁和最多三次真实 Provider 反馈修订；修复提示词安全清洗把内部章节正文统一截断到 1500 字的问题，内部 `_chapter_body` 保留注入过滤并放宽到 12000 字。
- **证据**：最终章节 11 段、约 2630 正文字符；32/32 条 `ai_calls` 成功；96 条审计事件保留。隔离验收书与章节、9 条知识项已按精确 ID 软删除，运行与调用账本保留。
- **代码与门禁**：主线提交 `bf1a377`（此前修复链 `44eac3c`、`eb6c72b`、`ac225af`、`220f9a5`）；本地后端 `657 passed, 9 skipped, 1 xpassed`；GitHub Actions run `30330421031` 的 backend/security/frontend/frontend-test/e2e 全绿；生产 `https://novel.xyjin.xyz/api/v1/healthz` 返回数据库、Redis、worker、DeepSeek 配置全部正常。
- **边界（T5 实测）**：本次证明单本完整创作主链可用且已通过真实 Provider 验收；不等同于百章长跑、真实内容平台发布回执或所有外部热点源均已验收。

## 2026-07-17 V2.0 全面升级

### 用户10问题整改

| # | 问题 | 状态 | 证据 |
|---|------|:--:|------|
| 1 | 番茄全榜 | 🧪 | /api/rank/list 7本巅峰榜 + 37分类API 1117本。Playwright已集成但VPS内存不足跳过 |
| 2 | 起点/纵横100+ | 🧪 | 起点7榜合并44本（分页不生效），纵横33本。默认200可配环境变量 |
| 3 | 编辑器排版 | 🧪 | novel-prose.css(440行) + RichEditor.tsx重写(329行)。CSS已打入Docker bundle |
| 4 | 编辑器UI | 🧪 | Editor.tsx增强(263行)：左目录/AI面板/状态栏/全屏/夜间/专注 |
| 5 | 去AI味 | 🧪 | deai_pipeline.py(223行)七层管线 + deai.py API(165行)。未真实DeepSeek验证 |
| 6 | 热点多平台 | 🧪 | 8平台fallback URL。B站API实测通过。头条/小红书/抖音需代理 |
| 7 | 热点文库 | 🧪 | articles CRUD在hotspots.py + HotspotDashboard文库Tab |
| 8 | 书库删除 | ✅ | 7端点(单本/批量/章节删除) + 前端checkbox/确认弹窗；前端测试与 bundle 实测 |
| 9 | 十层分析 | 🧪 | ten_layer_analysis.py(793行) + /ranking/analyze端点。platforms自动去重 |
| 10 | 选题池 | 🧪 | bookmark/delete/batch-delete端点 |

### 新增功能

| 功能 | 文件 | 状态 |
|------|------|:--:|
| DashboardV2 首页 | DashboardV2.tsx(613行) | ✅ |
| 全局玻璃拟态主题 | global-v2.css(413行) + ThemeProvider；前端构建测试通过 | ✅ |
| 仿写HTML提取 | imitation.py _extract_text() | ✅ |
| 一键采集所有平台 | ranking.py /scan-all + 前端按钮 | ✅ |
| 多平台聚合分析 | ranking.py /analyze + 前端UI | 🧪 |
| 新平台: SF轻小说 | ranking_adapter.py fetch_sfacg_ranking() 43本 | ✅ |
| 新平台: 潇湘书院 | ranking_adapter.py fetch_xxsy_ranking() 41本 | ✅ |
| 平台替换: QQ阅读/晋江 | 替换七猫/17K/刺猬猫(JS-SPA不可采)；适配器测试覆盖 | ✅ |

### 验证基线

- 后端: `pytest` → 565 passed, 9 skipped
- 前端: `npm run build` → ✓ built (tsc -b + vite)
- 部署: VPS Docker frontend + systemd API → 200 OK
- 提交: main @ c5b3463
>
> **治理说明**：此前版本宣称"20/20 任务全部完成、6/6 深度融合、8/8 整合、真实 Provider T3/T5 通过、真实源 7 天稳定运行、浏览器 E2E 验收通过"，均无《23》§6 要求的验收证据（commit/命令/日期/输出），且"7 天 T5"与文档同日更新在时间上不可能成立。按《23》§8 全部降级为证据可查的状态；重建证据台账前不得恢复 ✅。外部审计报告（2026-07-11）的 §三/P1-6 亦指认了同一问题。
>
> **历史门禁记录**：本节当时使用 `docs/NovelCraft-开发文档/23-AI开发边界与交付真实性规范.md`。当前开发以 `docs/Starlume-AI-开发文档/05-AI遵从与开发真实性规范.md` 为权威入口。

## 当前主线 (main)

### 2026-07-17 WIP 收尾入库 + main 合并 + 整体部署

- **WIP 收尾（@a6ed0da）**：上一会话遗留后端 WIP 逐项审查后入库——章节长度硬门禁（3 次真实重试带反馈）、七维质量证据持久化到章节 meta、移除 analyze_hotspots 模板伪 AI 输出、gateway editor/style 空输出防护、人工选名门禁（"待命名作品"占位）、`/runs/latest` 恢复端点、智能体 stale 状态识别、书库字数修真、知识库嵌入连接泄漏修复、versions.reason TEXT 迁移、nginx /api 直连（线上已提前生效，repo 补记）。
- **合并 main（@27249e4）**：榜单三修复（起点 script tag/番茄 snake_case/番茄全分类）+ CI allowlist 并入分支；overseas_complete.py 冲突取分支侧（审计整改后继版），同步清理 main 侧遗留的 generate_consistency_report 死豁免条目。
- **验证**：合并树全量 `pytest` → **565 passed, 9 skipped**（本地需 `NOVELCRAFT_TEST_REDIS_URL=redis://localhost:6379/15`，否则 fail-closed 503 导致误报）；truthfulness/delivery 门禁通过；前端构建通过。
- **部署**：生产服务器从 main+分支前端的混合状态切换为整体跟随分支 @27249e4，api/worker/beat/frontend 全部重建；基础设施侧确认用户已清理家中 frpc 重复 `nc-proxy` 段（frps 日志 0 次 port already used，代理 200）。

### 2026-07-16 全量回归 + 生产浏览器巡检与修复轮

- **全量回归**：本地 `pytest` → **565 passed, 9 skipped**（CI 等价环境；此前一次失败为本地 `DEEPSEEK_MODEL` 注入所致误报，CI 环境复跑通过）；`npm run build`（tsc+vite）通过；`verify_ai_truthfulness.py`、`verify_delivery_claims.py` 通过；alembic 单头 `nc_versions_reason_text` 全量迁移通过。
- **生产浏览器巡检（真实登录态，Chrome 驱动）**：19 个页面全部渲染并核对控制台/网络请求；扫榜中心真实起点/番茄快照与失败详情展示正常；书库、审阅七维雷达、编辑器 TipTap、成本追踪 13 次真实 DeepSeek 调用、智能体任务计数等均为真实数据。
- **修复 1（生产配置）**：服务器 `.env` 缺 `NOVELCRAFT_ADMIN_EMAILS` → `/admin/settings` 403 且前端产生未捕获异常；已补配并按 `docker-compose.prod.yml`（env_file 全量注入）正确重建 api/worker，登录态实测 200。
- **修复 2（后端 @9c4abc1 main / @535dea6 分支）**：`GET /admin/settings` 由写级 `require_admin` 改为 `require_admin_reads`（QA-003 口径，与 providers/model-routes/prompts 一致）。
- **修复 3（前端 @d72f2cf 分支）**：run 终态（succeeded/failed）停止 2 秒轮询；切换 Tab 滚回顶部；Settings 管理接口失败如实提示而非未捕获异常；同轮把此前已构建部署到生产（bundle `index-D_diNxCO`）的前端 WIP 入库补齐溯源。
- **修复 4（基础设施）**：热点采集全源超时根因定位——`jp.xyjin.xyz:8888`（frps 隧道→国内 tinyproxy）被互联网扫描者当开放代理滥用打满，且 frps 存在僵尸 `nc-proxy` 注册占用端口；已 ufw 白名单收紧 8888 仅允许 43.156.17.78、重启 frps 清除僵尸注册。实测 `/api/v1/hotspots` 恢复 200、60 条真实百度热点，看板正常渲染。遗留：国内 frpc 配置存在重复绑定 8888 的 `nc-proxy` 段，建议清理；知乎/微博 ajax 需有效 Cookie。

- **创意拆解模块化**：所有新书统一执行“原始需求 → 创作圣经 → 市场/故事/玩法/世界/人物/冲突规划”，不再把排行榜建议标题当成已确认标题。规划结果包含 `source_facts`、`design_additions`、`forbidden_changes`、长篇阶段、篇幅和内容配比。
- **规划忠实度硬门禁**：`plan_idea` 每次真实 DeepSeek 输出后，新增独立 `audit_plan_fidelity` 调用逐项对照原始需求；年龄、年份、职业、设备、目标字数、内容主次和前三章事件存在矛盾/遗漏时，自动反馈并重新调用真实 AI，最多三轮；三轮仍不合格则节点 `failed`，不得进入选名或继续写作。用户要求后续生成的分卷总纲、前 N 章细纲、章节正文等记录在结构化 `downstream_deliverables`，由后续模块执行，不在策划节点伪装已产出。每轮策划与审计均写 `ai_calls`，没有 mock/fallback。
- **人工选名**：面向用户的开书入口固定在 `human_confirm_title` 停下，展示 3–10 个候选；支持重新生成候选、填写反馈、自定义书名。用户确认前不进入蓝图和章节生成。
- **当前证据**：相关回归 38 passed；全量后端 `557 passed, 9 skipped`；后续幂等恢复专项 24 passed。生产 run `9a5a00b7-f806-43db-9f8a-452a86211966` 使用 DeepSeek `deepseek-v4-pro` 完成三轮“策划→独立审计”纠偏，最终 `score=100 / contradictions=[] / omissions=[]`；随后市场、故事模式、核心玩法、世界观、人物、冲突图谱均以 attempt 级新幂等键重新真实调用，工作流停在 `waiting_human/human_confirm_title`，未自动选名。内置浏览器与当前 Chrome 均无 NovelCraft 登录态，只能到登录页，因此登录后 T4 视觉验收仍未完成，当前状态保持“已接线 / 生产 T3 已验证”。

### 2026-07-14 待验收推进轮证据（部分完成/骨架 → 待验收）

按《24》顺序推进，每模块独立提交并绑定测试证据；全部状态最高为 🧪 待验收，未标任何 ✅：

- **NC-LIB-002/003（@9d1786f）**：`/library/books` 服务端检索(q)/状态筛选/白名单排序（含 SQL 注入回退负例）；前端「最近编辑」排序；编辑器 3s 防抖自动保存（脏检查、冲突处理期间暂停）。`test_library_contract.py` 2 passed。
- **NC-HM-001/002/003（@9d1786f）**：hm_content 4 个真实 AI 函数首次获得产品入口（platform-match/title-variants/video-script/material-suggestions + Dashboard 选题工具箱）；去重/趋势/时效评分与可恢复工作流回归。`test_hm_media_pipeline.py` 7 passed。
- **长篇一致性（@7481791）**：卷级门禁 + 批量生成 409 enforcement + 百章真库压力 + 摘要失败显式化。`test_long_novel_consistency.py` 7 passed。
- **发布中心（@df778e4）**：指标回流 sweep（beat 6h）+ 状态机/60s 调度行为测试。`test_publish_center.py` 4 passed。
- **融合与骨架项目（@4a27eb9）**：事件账本/事实链负载修复与产品端点、事实事务接入真实 reconcile、分层规划/审计/humanize/insprira/BrowserAct removed 诚信回归。`test_skeleton_fusion_projects.py` 5 passed + `test_fusion_deep.py` 行为对照补强。
- **诚信机器门禁（本轮）**：新增 AST 级 `scripts/verify_ai_truthfulness.py` 并接入 `scripts/ai_development_gate.sh` 与 GitHub Actions；拦截 AI 命名函数绕过 gateway、硬编码 `wired/available/status=active`、固定伪生成模板。新增 `test_truthfulness_gate.py` 负例，确认坏代码会被挡下。
- **融合状态证据驱动（本轮）**：`/fusion/status` 的能力状态由当前用户项目最近 30 天 `ai_calls` / workflow audit evidence 倒推：`verified` / `wired_unverified` / `missing` / `removed`；没有真实成功记录不再计入 verified。新增回归断言 `book_analysis` 成功记录可把 book analyzer 从 `wired_unverified` 升为 `verified`。
- **NC-SEA 出海审计整改（本轮）**：补齐 `translate_segment`/`cultural_localize`/`localize_names` 输出契约与 `overseas.localize_names` prompt；合规检查由裸关键词升级为确定性规则引擎（多语言/别名 pattern，返回 matched_rules）；`publish_overseas` 写入真实 project_id；`/translate` 与 `/overseas/publish` 端点补项目/内容权限和 Provider 失败显式 502；新增 `test_overseas_complete.py`，覆盖合规、术语覆盖、发布项目血缘、API 权限、真实 DeepSeek 翻译 provenance。
- **NC-HM-001 历史热点回填（本轮）**：新增 `/hotspots/history/backfill` 与 `/hotspots/history`，支持在“设置 → 平台连接”或环境变量配置 `history_url`（支持 `{date}`）后按真实授权归档源回填近 7 天热点快照；无历史源时返回 `unsupported`/502，不用当前热点伪造历史。`test_hotspot_history_backfill.py` 覆盖 7 天日期血缘、跨天同标题保留、无源不落库、接口 502。
- **NC-SC-001 扫榜采集闭环（本轮）**：新增 `/ranking/capture-import` 与 `/ranking/snapshots/{id}/confirm-capture`；前端导入 JSON 可自动识别番茄 OCR / 起点用户会话 / 可见浏览器采集工件，保留 screenshot/artifact/置信度证据；低置信 OCR 自动标 `needs_review` 并阻断 AI 分析，owner/editor 确认后放行；起点 challenge / 番茄 OCR required 等非成功状态形成失败快照，不返回空成功。`capture_ranking.py` 已升级为自动优先：DOM 提取 → 番茄私有码本机 Tesseract OCR 尝试 → 缺 OCR/挑战/结构漂移输出显式工件，支持 `--headless --no-pause` 调度；2026-07-14 真实公开源纵横 headless 采集成功 34 条，生成本地证据 `var/ranking-captures/zongheng-live.{json,png,html}`；起点 headless/no-pause 真实复验输出 `user_action_required`（腾讯验证码页），番茄 headless/no-pause 输出 `schema_changed`（App 壳未暴露榜单卡片），均生成 JSON/PNG/HTML 证据且未伪装成功；本地证据目录已 gitignore。
- 验证命令：`cd backend && .venv/bin/pytest -q` → **549 passed, 9 skipped**（2026-07-14）；`python3 scripts/verify_ai_truthfulness.py` → passed；`npm --prefix frontend run build` → passed；`set -a; source .env.local; set +a; npm --prefix frontend run test:e2e` → **5 passed**（含真实 DeepSeek 主链②，上轮证据）；`GATE_ALLOW_WARNINGS=1 bash scripts/ai_development_gate.sh` → AST truthfulness passed，旧宽泛扫描仍需逐项解释。

### 2026-07-14 本轮整改证据

- P0 成本追踪白屏：已改为读取 `/api/v1/admin/budgets` 与 `/api/v1/admin/model-routes` 的 `response.data`，组件侧增加数组保护；证据：`npm --prefix frontend run test:e2e` 通过，含 `主链①d：成本追踪页无白屏并展示预算与模型路由`。
- P1 自媒体生成真实 AI 化：`hm_content` 的文章、标题、短视频脚本、素材建议均走 `app.gateway.complete()`；新增/补齐 `gen_daily_brief`、`hm_daily_brief`、`hm_title_variants`、`gen_video_script`、`hm_material_suggestions` 输出契约；失败按网关错误暴露，不返回模板成品。
- P1 融合状态诚信：`fusion_governance` 不再硬编码 active；`BrowserAct.chrome_publish` 明确为 removed；`fusion.py` 中 Deep Workflow / Deep Book 状态按真实导入与替代模块可调用性计算。
- P2 embedding：remote/local 失败不再静默切 hash；hash 仅允许测试/显式离线开发模式，产品路径缺少语义后端会直接报错。
- 验证命令：
  - `cd backend && .venv/bin/pytest -q` → `493 passed, 8 skipped`
  - `npm --prefix frontend run build` → passed
  - `set -a; source .env.local; set +a; npm --prefix frontend run test:e2e` → `5 passed`（含真实 DeepSeek 主链②）
  - `bash scripts/ai_development_gate.sh` → 关键项通过；剩余宽泛警告为 UI placeholder、`fallback_json` 历史字段名、空集合自然返回等，不能作为完成声明证据，交付时需继续说明。

| 能力 | 状态 | 已有证据 | 尚未覆盖 |
|---|---|---|---|
| 榜单采集 adapter | 🧪 待验收 | 纵横官方页真实采集 T3；番茄/起点失败显式化；CSV/JSON/浏览器/OCR 采集工件导入、低置信拦截、人工确认放行、Open Library 六态校验均有真库测试 | 真实番茄截图/OCR样本与起点已登录会话实跑；真实源长周期 T5 |
| 榜单中心 | 🧪 待验收 | 扫榜、快照、分析、选题、成书前后端入口；导入与元数据校验 UI | 真实 AI 分析 T3、完整 E2E |
| 原创市场选题 | 🧪 待验收 | Gateway、严格 Schema、防注入、Provider 失败直接 `failed`/HTTP 错误契约测试 | 真实 Provider T3 输出证据 |
| 扫榜自动建书 | 🧪 待验收 | 幂等键建书、来源血缘、跳过人工选名 | 真实 Provider 批次成功路径 |
| 连续章节流水线 | 🧪 待验收 | 严格 Schema、章节幂等键、连续性风险报告、批次 `failed`/resume 断点续跑；浏览器实测失败→恢复链与导出正文 | 真实 Provider 多章样本与批次成功路径 |
| 统一书库 | 🧪 待验收 | 书库 API/页面、检索/筛选/排序、目录导入（权限+seq+幂等落库）、导出 TXT/MD | 完整 E2E 验收 |
| 灵感 Bootstrap | 🧪 待验收 | 灵感→书名→设定→第一章→审核基础链；真实 Provider V2 全链 `test_real_provider_v2_bootstrap.py` 已跑通（2026-07-14） | 产出质量人工验收 |
| 热点自媒体 | 🧪 待验收 | 采集器去重(24h窗口)/趋势/时效评分 + 趋势报告端点；平台匹配/受众/合规风险、AI 标题变体/短视频脚本/素材建议 4 新端点+Dashboard 选题工具箱；批次失败回滚零残留→重试幂等键复用（`test_hm_media_pipeline.py` 7 tests，@9d1786f）；历史归档 URL 配置后可按真实源回填 7 天快照（无源 502，不伪造） | 真实授权历史源配置与 7 天调度稳定性；成稿质量人工验收；真实平台发布回执 |
| 长篇一致性 | 🧪 待验收 | 真库百章写前检索窗口(<5s)、写后 reconcile 接线+新实体标记、卷级门禁（缺章/未过审/到期伏笔/实体矛盾4类阻断，批量生成 409 enforcement，通过时生成卷摘要落库）；摘要 AI 失败显式化不再落伪摘要（`test_long_novel_consistency.py` 7 tests，@7481791） | 真实 Provider 百章 T5 长跑 |
| 发布中心 | 🧪 待验收 | Fernet 凭据、状态机全生命周期+非法迁移拒绝、beat 60s 到期派发+6h 指标回流 sweep（只聚合真实采集数据）（`test_publish_center.py` 4 tests，@df778e4） | 真实平台发布回执与真实回流数据（需有效平台凭据） |

## 八上游项目融合

| 项目 | 状态 | 真实边界 |
|---|---|---|
| oh-story-claudecode | 🧪 待验收 | 7 上游 Skill+许可证在库；PromptSpec/golden cases 播种并有汇总校验（`test_fusion_deep.py` golden case 断言、`/batch` golden-case 检查端点）；write_chapter_draft/final_humanize 绑定方法论提示词（`test_fusion_wired.py`） |
| denova | 🧪 待验收 | 事件账本行为对照测试（负载 roundtrip、非法事件类型拒绝、detail/details 键名错位回归修复）；run 账本查询端点 `/runs/{id}/ledger`；workflow plan 由 config+dag_exec 产品链承担，无调用的 WorkflowPlan 助手类已删（@4a27eb9） |
| show-me-the-story | 🧪 待验收 | 章节事实事务链接入真实 reconcile 落库路径（每次 reconcile 写可回溯事务），查询端点 `/contents/{id}/fact-chain`，roundtrip 测试钉住 previous/new 负载（@4a27eb9） |
| AI_NovelGenerator | 🧪 待验收 | 目录解析/四阶段20节点全链 T2；真库百章检索窗口与一致性组件回归（`test_long_novel_consistency.py`）；真实 Provider 百章 T5 未做 |
| AI-auto-generates | 🧪 待验收 | 批量生成批次幂等恢复、书本分析真实 Gateway（`/books/analyze` 写 ai_calls）、章节目录导入落库均有回归 |
| harnessNovel | 🧪 待验收 | 分层规划 `/novels/layered-plan` 走真实网关（空响应显式失败）；合理性审计/humanize 由 bootstrap 末段节点承担且次序钉死（`test_skeleton_fusion_projects.py`，@4a27eb9） |
| BrowserAct (MIT) | 🧪 待验收（合规缩减版） | 按《25》仅保留用户已登录会话半自动发布包装；anti-bot 已删除，fusion 状态如实上报 removed（有回归测试钉死） |
| insprira (AGPL 洁净室) | 🧪 待验收 | 账号追踪幂等 + 真库诊断计算（发帖数/互动均值/redfox 指数/评级）、违禁词三类命中与洁净放行回归；check-compliance 改 JSON body；真实平台数据验收仍需真实账号 |

## 验证基线（2026-07-14 本轮工作树实测）

- 后端测试：**549 passed，9 skipped**（2026-07-14 本轮复跑，本地真实 DeepSeek key；真实 Postgres）。业务运行时不再提供 mock provider。新增覆盖：仿写相似度红线/版权提示、热点生成持久化/回滚、书库正式路径、出海翻译项目归属、平台连接可视化配置、审计报告 P0/P1 整改、诚信门禁、融合证据驱动、NC-SEA 合规/术语/发布血缘/真实 DeepSeek 翻译 provenance、热点 7 天历史归档回填协议与 unsupported 失败语义、扫榜浏览器/OCR采集工件导入与确认门禁。当前 Provider 验收口径：暂以 DeepSeek 为唯一必需真实链；Claude/OpenAI/Gemini 待用户提供对应 key 后再验收。
- 前端：`npm run build` 通过（仅保留 Vite 关于 `api.ts` 动静态混合导入和空 `react` chunk 的既有警告）。
- Alembic：单头 `nc_audit_workflow_scope`；本地库 upgrade→downgrade→upgrade 往返通过。
- 浏览器实测（上一轮）：审阅页时间线/人物弧线渲染真实数据；设置页数据统计为真实计数（AI 调用/内容数/pg_database_size）。当前整改要求 AI provider 失败直接失败/报错，不再把 provider 失败作为可伪恢复的成功态。
- **真实 Provider T3/V2/新增功能（2026-07-14，deepseek-chat）**：本地开发 key 仅保存在 `.env.local`（Git 忽略）。`test_real_provider_t3.py`、`test_real_provider_v2_bootstrap.py`、`test_real_provider_new_features.py` 均已跑通。新增功能真实冒烟覆盖：编辑器整章重写→七维评分→下一章规划、热点生成公众号草稿落库、仿写生成原创样稿并返回相似度报告。
- **浏览器自动化 E2E（2026-07-14 复验）**：Playwright `npm run test:e2e` 实测 **4 passed, 26.2s**。用例①注册→CSV 导入→快照落库→刷新持久→书库空态；用例②书库详情页可进入并展示简介/最新章节/全部章节；用例③平台连接可视化填写、加密保存、不回显密钥；用例④真实 AI 分析→原创选题→建书→书库可见。
- **审计报告整改（2026-07-14，commit 后工作树）**：针对《审计报告_NovelCraft_2026-07-14.md》新增 `tests/test_audit_report_remediation_20260714.py`，9 条专项回归通过；受影响老回归 77 条通过。`knowledge/daily-briefing` 仅消费真实采集/已采集热点，不再由 AI 编造当前热点；`/books/analyze` 改为真实 Gateway `book_analysis` 并写 ai_calls；worker `_track_budget` 由 ai_calls 重算并同步 budgets；发布 worker/一次性发布读取 Fernet 存储凭据；BrowserAct.chrome_publish 标 removed；移动端媒体查询改为真实 `.layout`；`.env.example` 补齐凭据/DB/Redis/Provider/热点/告警配置。
- T5 长周期运行：仍无 7 天连续调度证据，未验收。热点模块已支持授权历史归档 URL 回填 7 天数据；这只能证明“可回填真实历史源”，不能替代“连续 7 天稳定采集”验收。

## 审计对照（外部审计报告 2026-07-11）

- 已修复（`efba333`）：B1 JWT 生产强校验、B2 管理员绕过收口、B4 裸 fetch 清除、P0-1 DB.close 改 rollback、P0-2 SSE 真换行、P1-4 死组件接线。
- 已修复（本轮）：B3 发布凭据 Fernet 加密落库（`platform_accounts` 启用，响应不回显凭据）；P0-3 compose api/worker 依赖 migrate 完成；P1-1 修复 3 条静默失败索引（错误列名，原迁移 `except: pass` 吞错）；Redis appendonly + 持久卷（更正：`7053a07` 提交信息声称 "Redis persistence" 但未实际配置，属《23》§4 虚假上报，本轮补齐）；F5 设置页假统计→真实 `/stats/overview`；F7 审阅页恒空时间线/弧线→真实 `/novels/{id}/narrative`；F8 敏感词前端空函数→真实 `/contents/{id}/check-sensitive` 并接入发布前置检查；F12 DagEditor 空 project_id→真实项目 ID 且缺失时拒绝保存。
- 已修复（第二轮，对照审计全量版 §六）：多轮审核/跨模型审计/Prompt 矩阵此前用字符串长度公式伪造评分并宣称"ready"，现真实经 Gateway 调用、Provider 不可用逐项 `failed`，端点补 `project_id` 成员校验；热点采集 `except: continue` 静默空成功改为逐源状态+全失败 502；AgentConsole 硬编码"模拟"数据改为 `/agents/status` 真实 run_nodes 聚合；Collaboration 页面调用不存在的路径（必 404）改为真实 `/collaboration/*`；`/admin/workflows/{name}/execute` 此前无视工作流名一律跑 bootstrap 且 `project_id=''` 必然崩溃，改为权限校验+仅 bootstrap 可执行+其余显式 501；AI 编辑补 `ai_edit` 版本分支（C5-03）；C5-05 自动保存 7 天保留 beat 任务（保留每实体最近 10 份，语义分支永不清理）；assembler 知识层此前按无人写入的列过滤永远为空，改为按小说前提走 Knowledge Hub 检索。
- 已修复（第三轮，代码/文档/功能契约对账）：工作流保存请求原本必 422 且代码引用不存在的 `workflows.project_id/config`，现新增项目作用域迁移并统一使用 `definition`；系统 Bootstrap 只读，自定义 DAG 明确为设计稿、未接执行器返回 501；清除预算/日报/翻译四组重复路由及其中的跨项目翻译风险；设置页知识导入/导出与预算、知识检索、热点响应、Fanout 响应、多平台发布均对齐真实 API；短篇输入真实落库；Fanout Provider 失败不再复制原文冒充改写成功。
- 已修复（第四轮，按《27-全仓库审计报告》路线图阶段 0）：P1-A 迁移回滚断裂——性能索引 downgrade 移除 CONCURRENTLY，干净库 upgrade→downgrade base→upgrade 三段实测通过，并加全迁移源码契约测试；P1-B schema 快照契约测试（12 张核心表必需列钉死，防列名漂移复发）；P1-G 交付门禁升级为证据绑定校验（✅/已交付行必须含测试/commit/文件/T级标记，含负例测试）；P1-D 不可信外部文本统一清洗 `sanitize_untrusted`（接入 assembler 知识召回与热点晨报入模路径）；P1-E 备份 pg_dump 每日 sidecar + 7 份保留 + 全服务日志轮转（compose，YAML 校验通过）；P2 清理：PublishPage 死组件删除、`store_ranking_snapshot` 吞错死函数及其 T0 存在性断言删除、bundle 分包（主 chunk 702KB→281KB，消除 >500KB 警告）、版本统一（app `2.2.0` 对齐需求基线 V2.2，README 刷新）。**396 tests 全绿**。
- 已修复（第五轮，本轮审查）：纯文本编辑增加 DeepSeek SSE、完成后统一写 ai_calls/版本；流式鉴权支持 token 刷新，预算与 Provider 错误分流，非 DeepSeek 路由安全回退普通网关；Embedding 增加 remote/local/hash 三层适配、来源标记和项目重建，远端部分/非法响应回退 hash，不同后端向量禁止混算；Sentry/Prometheus 可选接线，巡检增加队列积压和成本日报。代码验证 417 passed、前端 build；真实流式 Provider、local semantic 和 Sentry 送达仍未作为验收证据。
- 全面复核（2026-07-12 第二轮，@068a45d）：PR#4 加固复核通过（流式 PENDING_BUDGET 语义/非 deepseek 显式拒绝/embedding 数量与有限性校验/检索按 provenance 过滤）；T5 提交内的一致性组件修复复核通过——entity/summarizer/timeline 此前把 AI 调用记账到 `SELECT id FROM projects LIMIT 1` 的**任意项目**（跨项目成本/预算污染），已改为按 content 归属项目；伏笔抽取 `hint_chapter` 字段对齐 prompt 契约；章节富化失败不再阻断落库与审核门禁。新增实测验收：**流式端到端真实 Provider 通过**（HTTP SSE 经 8100 API，逐字增量帧+done 帧，DeepSeek 真实调用）；**local 语义质量验收通过**（EMBEDDING_SEMANTIC_TEST=1，bge-small-zh 真实模型："地底嗡鸣"≈"深渊低语"≫无关文本，余弦差 >0.1，本机含首次模型下载 34s 完成 8/8）。
- 仍开放：B4 Nginx 无 TLS（需域名/证书决策）；流式浏览器端验收；remote embedding 质量验收（需 embedding API key）与全库重建演练；数据回流/ROI 真实数据；Sentry 与告警实测送达（需 DSN/Telegram env）；发布真实平台回执与 auto_publish 调度。真实平台账号/API/人工输入项已新增“设置 → 平台连接”可视化配置入口，敏感字段加密保存且不回显；海外 RoyalRoad/WebNovel/KDP 等真实发布暂不做自动发布，只保留 manual_required 草稿/人工回执路径，后续真实回执依赖用户填入合法平台凭据或人工上传凭证。`workers/tasks.py` 拆分；Agent 注册表仍为声明式（无独立执行体）；task/日级预算分级；T5 百章长跑进行中（完成后补证据）。

## 下一顺序

以《24-AI开发任务列表》为准：优先补真实 Provider T3 证据（解锁全部"待验收"），再做 E2E 脚手架与 T4 用例，随后按序推进 HM/PUB/SEA。任何状态回升必须绑定同一提交中的验收证据。

## 2026-07-13 深度融合轮（经产品级验收审计修正）

> **治理更正**：本节此前把六项目融合标为"完成"并宣称"19/19 节点端到端通过、445 passed"，但《NovelCraft验收审计报告》（@53a3857，真实库实测）证明当时 18 个新 task_type 在 model_routes 与 prompts 中命中均为 0/18、默认模型为虚构的 deepseek-v4-pro——主打工作流属未接线门面，"19/19"依赖手填模型且提示词为 15 字兜底，"445 passed"跑在另一分支。按《23》§8 原声明作废，以下为整改后带证据的状态。

### 审计问题整改（2026-07-13 第四轮，同一提交链）

| 审计编号 | 问题 | 整改与证据 |
|------|------|------|
| BUG-01 🔴 | 18 新节点 0/18 路由、0/18 提示词 | db.py 播种 18 条 model_routes + prompt_registry 补 18 套方法论提示词（200-700字，绑定上下文变量）；真实库实测 gateway 解析 **18/18 有效**（deepseek-chat + 非兜底提示词）；回归锁定 `tests/test_v2_bootstrap_wiring.py`（10 tests） |
| BUG-02 🔴 | 虚构模型 deepseek-v4-pro/flash | gateway.MODEL 与 config.deepseek_model 改 `deepseek-chat`；init_db 自愈存量 v4 路由行；仓库全文 0 处 v4 残留（wiring 测试钉死） |
| BUG-03 🔴 | nc_commerce_plans 外键类型不匹配，全新库迁移崩溃 | subscriptions.user_id VARCHAR(36)→UUID；干净 pgvector/pg16 实测 `upgrade head→downgrade base→upgrade head` 三段通过 |
| BUG-04 🟠 | Alembic 双 head | 新增 `nc_merge_commerce_head` 合并；`alembic heads` 单头 |
| BUG-05 🟠 | 新节点无输出契约（垃圾输出算成功） | 18 个 pydantic 输出模型（宽容 extra、必填字段严格）+ 18 条 OUTPUT_CONTRACTS；负例测试拒绝畸形输出 |
| BUG-07 🟡 | 无 Key 静默走错误默认 | healthz 暴露 `ai_key_configured`（仅布尔），创作向导无 Key 时显示引导警示 |
| 门面追加 | fusion.py 路由从未挂载（/fusion/status 恒 404） | 已挂载 `/api/v1/fusion/status`，`tests/test_fusion_wired.py` 实测 200 |

### 六项目融合状态（整改后）

| 项目 | 状态 | 证据（真实编排/落库/校验链；AI 需真实 provider 验收） |
|------|------|------|
| oh-story-claudecode | 🧪 已接线待真实验收 | 7 SKILL 文件+LICENSE 在库；write_chapter_draft/final_humanize 提示词绑定 story-long-write/deslop 方法论；`test_fusion_wired.py` 钉死 |
| AI_NovelGenerator | 🧪 已接线待真实验收 | 四阶段 19 节点全链 T2 通过（`test_v2_bootstrap_full_flow.py`：建书→规划→人工确认书名→蓝图→写作→六维一致性→落章+知识库+账本） |
| harnessNovel | 🧪 已接线待真实验收 | 分层规划（plan_*→blueprint_*）在全链测试中逐节点 succeeded |
| show-me-the-story | 🧪 已接线待真实验收 | write_fact_reconcile 节点 + `_write_after_reconcile` 在全链测试中执行 |
| denova | 🧪 已接线待真实验收 | Event ledger（audit_logs）记录 run.started/checkpoint/run.completed 有断言；provider 失败按当前规则进入 failed/报错，待真实 provider 长链验收 |
| AI-auto-generates | 🧪 已接线待真实验收 | 批量生成批次幂等恢复、书本分析真实 Gateway、目录导入落库均有回归（2026-07-14 待验收推进轮） |

### 验证基线（2026-07-13 本轮实测）

- 干净库：Docker pgvector/pg16 → `alembic upgrade head` 一次通过（修复前必崩）→ init_db 播种 52 routes/53 prompts/0 虚构模型。
- 决定性复验（审计同款检查）：18/18 新节点路由命中、18/18 提示词命中、gateway 实解析 18/18 真实模型+方法论提示词。
- 全链 T2（历史）：曾使用 mock provider 验证真实 Postgres 编排/落库/校验链。当前规则已移除业务 mock provider，该证据仅保留为历史参考，不再作为完成证据。
- QA 遗留：QA-002 微博嵌套解析（回归测试）/QA-003 admin 读接口按 NOVELCRAFT_ADMIN_EMAILS 收紧（配置即生效，个人实例不锁死）/QA-004 DELETE novels/contents 软删级联/QA-008 注册防枚举/healthz 增加 worker 存活+队列深度检查，均有 `tests/test_qa_remediation_20260713.py` 回归。
- **真实 Provider V2 全链**：`tests/test_real_provider_v2_bootstrap.py` 已用 2026-07-14 本地开发 key 跑通；该 key 不得提交，项目截止后回收。

## 2026-08-02 继续整改收口

- 已评估本地 `ai-workbench`，有效的情绪/钩子/分层去 AI/读者体验思路已落入 V7 生成与审稿链；评估文档为 `docs/AI_WORKBENCH参考评估_20260802.md`。
- 已补 V7 reader experience 五项证据、生成/续写情绪与跨章钩子约束、V7 novel→V6 project 映射迁移与权限校验。
- 本地数据库 Alembic 头为 `nc_v7_novel_project_mapping`，映射回填 6994 条；Prompt 新默认版本已播种。
- 最新回归：后端 843 passed/138 skipped/1 xpassed/2 warnings；前端 32 passed；E2E 最新复跑 17 passed/10 skipped/0 failed；构建、truthfulness、delivery claims、compile、diff check 通过。
- 仍未验收：真实 Provider 20 章双轨、跨版本账本真实对账、V6 书库/编辑器/导出回写和人工盲评；强制 gate exit 3 的宽泛告警保持记录。

## 2026-08-02 真实 Provider 双轨与产品链最终收口

- 真实本地 Provider 长跑已完成：V6/V7 各 20 章，V6 自动审核平均 79.6、最低 72.0；V7 平均 92.0、最低 91.0；V7 20/20 章有 `transition_contract`。
- V7→V6 真实链已核验：章节来源、运行 ID、状态、项目归属、版本记录均可追溯；编辑器首章、完成度、TXT、Markdown、EPUB 均成功，EPUB 为有效 ZIP。
- 统一账本已完成本地真实对账：369/369 成功、0 失败、3.190506 元；V6 232 条、V7 137 条；Prompt provenance 可按版本和执行记录追溯。
- 长跑中发现并修复批次提交、Provider schema 失败收口和 EPUB 依赖问题；修复后恢复运行完成，失败经历已保留在交接记录。
- 人工盲评包已生成 20 个匿名 case，但 0/20 case 有两位独立评审；因此自动双轨与产品链标记为**可用**，人工盲评为**已接线**，生成质量达标与生产 V6/V7 合并验收不能宣称完成。

## 2026-08-02 生产部署收口

- 提交 `7851b7f` 已推送 `origin/main`，生产机 `/opt/NovelCraft-Personal-Studio` 已 fast-forward 到该版本；远端原有运行时生成的 `backend/celerybeat-schedule` 未被覆盖。
- 发布前备份已生成：`backups/pre-deploy-7851b7f-20260802-180101.sql.gz`。
- 生产迁移已执行到 `nc_v7_novel_project_mapping (head)`；8 个 runtime Prompt identity 已成功播种。
- API、worker、beat、frontend、PostgreSQL、Redis 均正常；公网 `healthz` HTTP 200。
- `PROD_BASE=https://novel.xyjin.xyz backend/.venv/bin/python scripts/prod_smoke.py` 全部 15 项通过；生产浏览器走查 `BASE_URL=https://novel.xyjin.xyz npx playwright test e2e/prod-walkthrough*.spec.ts` 为 **4 passed**。
- 部署过程中首次 seed 因容器脚本导入路径缺少 `/app` 失败，已用正确 `PYTHONPATH=/app` 重试成功；该运行时问题已记录，不隐藏为无异常部署。
- 生产部署当前标记为**可用**；生产真实 20 章双轨和人工盲评仍未执行，不能把部署 smoke 当作生成质量验收。
# 2026-08-06 Demo 视觉骨架落地（本地工作树）

- 用户反馈“页面仍像旧版、不是 Demo”后，已将 Demo 的结构性视觉落地到主应用，不再只改颜色和空状态。
- 全局页头增加 NovelCraft 工作台面包屑；主导航不变。
- 首页增加真实 KPI 条、作品数据表、质量状态和目标进度；没有真实数据时显示空状态/未评分，不使用静态演示数据。
- 创作向导采用左侧步骤轨道 + 右侧表单/预览；书库、进度、扫榜、设置统一为深色墨蓝控制台面板并保留原功能链路。
- 验证：`npm run lint`、`npm run build`、`npm test -- --run`（49/49）、`e2e/pages-smoke.spec.ts`（1/1）通过；登录态浏览器已逐页检查首页、创作向导、书库、进度、扫榜和设置。
- 状态：**本地可用，未提交/未推送/未部署**。发布后还需做生产截图级验收。
# 2026-08-21 作者主导章节骨架工作区（本地已接线，待部署）

- 产品主流程已按人工主导定位切换：AI不再作为编辑器默认的整章写作者，改为生成单章700–1000字的章节骨架；正文继续由作者在中间编辑区人工完成。
- 后端新增真实 Provider 接口：`POST /api/v1/authoring/chapters/{chapter_id}/skeleton`、`GET /api/v1/authoring/chapters/{chapter_id}/skeletons`、`POST /api/v1/authoring/chapters/{chapter_id}/skeletons/save`。
- 骨架包含本章目标、主压力、3–6个场景节点、人物变化、主线推进、爽点/结果、伏笔、连续性警告和下一章钩子；骨架独立写入 `versions(entity_type=chapter_skeleton)`，不会覆盖正文。
- 编辑器新增“章节骨架”工作区：作者先输入本章灵感，生成后可人工修改并保存；顶部整章续写入口和正文工具栏整章候选入口已移除，保留选区润色/改写候选。
- 上下文仍只读取当前作品的已确认 Bible、V7 状态、故事线和伏笔；世界观侧栏按标题去重，避免重复导入条目污染工作区。
- 验证：后端作者工作区/契约/注入测试 `30 passed`；前端 `57 passed`；`npm run build` 通过；Python compile、`git diff --check` 通过。首轮前端测试误用了 Jest 参数 `--runInBand`，随后用 Vitest 正确命令重跑通过。
- 当前边界：本轮尚未用生产真实 Provider 生成骨架样本，尚未 commit/push/deploy；不宣称 Provider 质量、朱雀 95/5/0 或三章/20章长跑验收。

## 2026-08-21 作者主导章节骨架最新状态

- `0c95ae6` 已推送并部署到 `https://starlume.xyjin.xyz`；生产 healthz 为 `code=0`，数据库/Redis/Worker 正常；Prompt `authoring.chapter_skeleton` 已为 `1.1.0`。
- 首个真实 Provider 样本暴露了明确根因：Prompt 没有长度预算，DeepSeek 输出466可见字；服务端正确返回502且不写版本。已改为7个小节的可执行长度预算并部署。
- 修复后真实 Provider 样本：DeepSeek `deepseek-chat` 返回813可见字，独立骨架版本 `f01cd7df-6dad-4a42-9d2f-770a127f85eb`，`provider_verified=true`。
- 正文保护已核验：正文摘要 `790ae86e56200e2291496ffae4a761cf`、长度16602，生成前后不变；骨架版本独立保存，未覆盖正文。
- 本地回归：后端定向 `114 passed`；前端 `57 passed`；构建、compile、diff check、truthfulness、delivery claims 通过。
- 当前口径：骨架生成技术链**可用**；骨架质量多样本/人工评审、生产浏览器登录态验收、朱雀 95/5/0、三章/20章长跑、AI披露与正式发布闭环仍未验收。
