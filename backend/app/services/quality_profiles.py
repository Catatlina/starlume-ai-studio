"""Versioned, compact quality profiles for commercial Chinese web fiction.

The supplied writing packs are methodology sources, not runtime personas.  This
module converts their stable, reader-facing ideas into a small policy object:
platform expectations, genre/subgenre techniques, state ledgers and de-AI
checks.  It intentionally does not contain author names, quotations or copied
phrasing, and it keeps volatile platform claims out of hard gates.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .content_policy import content_generation_contract
from .pov_quality import THIRD_PERSON_NARRATIVE_POLICY, third_person_generation_contract
from ..v7.quality.failure_patterns import generation_constraints, failure_pattern_metadata
from ..v7.quality.payoff_strategy import select_payoff_strategy, strategy_metadata
from ..v7.quality.webnovel_strategy import (
    market_benchmark_directive,
    knowledge_source_metadata,
    resolve_webnovel_strategy,
    strategy_metadata as webnovel_strategy_metadata,
)
from ..v7.quality.report_distillation import (
    report_pack_metadata,
    render_report_directive,
    select_report_pack,
)
from ..v7.quality.readability_contract import render_readability_plan


QUALITY_PROFILE_SCHEMA_VERSION = "webnovel-quality-profile-v1"

READER_CHAPTER_BUDGET_SCHEMA_VERSION = "reader-chapter-budget-v1"
READER_CHAPTER_MIN_CHARS = 2200
READER_CHAPTER_MAX_CHARS = 3000

_PLATFORM_ALIASES = {
    "fanqie": "fanqie",
    "番茄": "fanqie",
    "番茄小说": "fanqie",
    "qidian": "qidian",
    "起点": "qidian",
    "起点中文网": "qidian",
}

_GENRE_ALIASES = {
    "都市": "urban",
    "现代": "urban",
    "都市小说": "urban",
    "urban": "urban",
    "玄幻": "xuanhuan",
    "东方玄幻": "xuanhuan",
    "仙侠": "xuanhuan",
    "修仙": "xuanhuan",
    "xuanhuan": "xuanhuan",
    "悬疑": "suspense",
    "灵异": "suspense",
    "suspense": "suspense",
    "科幻": "science_fiction",
    "science_fiction": "science_fiction",
    "历史": "history",
    "history": "history",
    "游戏": "game",
    "游戏电竞": "game",
    "game": "game",
    "洪荒": "fengshen",
    "封神": "fengshen",
    "洪荒封神": "fengshen",
    "fengshen": "fengshen",
}

_SUBGENRE_ALIASES = {
    "神豪": "urban_shenhao",
    "都市神豪": "urban_shenhao",
    "商战": "urban_business",
    "都市商战": "urban_business",
    "重生": "urban_rebirth",
    "都市重生": "urban_rebirth",
    "年代": "urban_rebirth",
    "年代文": "urban_rebirth",
    "异能": "urban_ability",
    "都市异能": "urban_ability",
    "高武": "urban_high_martial",
    "都市高武": "urban_high_martial",
    "脑洞": "urban_brainstorm",
    "系统": "urban_system",
    "都市脑洞": "urban_brainstorm",
    "传统升级流": "xuanhuan_upgrade",
    "升级流": "xuanhuan_upgrade",
    "传统玄幻": "xuanhuan_upgrade",
    "凡人流": "xuanhuan_mortal",
    "苟道": "xuanhuan_cautious",
    "苟道流": "xuanhuan_cautious",
    "长生": "xuanhuan_longlife",
    "长生流": "xuanhuan_longlife",
    "长生苟道": "xuanhuan_longlife",
    "长生苟道流": "xuanhuan_longlife",
    "史诗": "xuanhuan_epic",
    "史诗玄幻": "xuanhuan_epic",
    "宿命": "xuanhuan_destiny",
    "宿命流": "xuanhuan_destiny",
    "设定流": "xuanhuan_setting",
    "智斗": "xuanhuan_setting",
    "家族": "xuanhuan_family",
    "家族修仙": "xuanhuan_family",
    "系统流": "xuanhuan_system",
    "签到": "xuanhuan_system",
    # 番茄爽文激进策略
    "fanqie_aggressive": "fanqie_aggressive",
    "番茄激进": "fanqie_aggressive",
    "爽文激进": "fanqie_aggressive",
    "激进爽文": "fanqie_aggressive",
}

_STYLE_PLUGIN_ALIASES = {
    "长生": "xuanhuan_longlife",
    "长生流": "xuanhuan_longlife",
    "长生苟道": "xuanhuan_longlife",
    "长生苟道流": "xuanhuan_longlife",
    "系统赋我长生": "xuanhuan_longlife",
    "xuanhuan_longlife": "xuanhuan_longlife",
    "xianxia_longlife": "xuanhuan_longlife",
}


def reader_chapter_budget(
    profile: dict[str, Any] | None = None,
    requested_target: int | None = None,
) -> dict[str, Any]:
    """Derive a reader-facing chapter budget before prose generation.

    Platform limits are ceilings, not writing targets.  The report packs
    already contain soft chapter ranges for both the selected genre and
    platform; use their overlap as the recommended reader range and keep a
    small, explicit natural-variance allowance for the generation contract.
    Short synthetic/unit-test targets remain valid so the helper does not
    turn a deliberately small test fixture into a production-sized chapter.
    """
    active = profile if isinstance(profile, dict) else select_quality_profile()
    soft_metrics = active.get("report_soft_metrics") or {}
    genre_metrics = soft_metrics.get("genre") or {}
    platform_metrics = soft_metrics.get("platform") or {}

    def _range(value: Any) -> tuple[int, int] | None:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return None
        try:
            low, high = int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
        if low <= 0 or high < low:
            return None
        return low, high

    genre_range = _range(genre_metrics.get("chapter_chars_target"))
    platform_range = _range(platform_metrics.get("chapter_chars_target"))
    if genre_range and platform_range:
        low = max(genre_range[0], platform_range[0])
        high = min(genre_range[1], platform_range[1])
        if low > high:
            low, high = genre_range
    else:
        low, high = genre_range or platform_range or (1800, 2800)

    requested = max(1, int(requested_target or 3000))
    short_override = requested < low
    effective_target = (
        requested
        if short_override
        else min(max(requested, READER_CHAPTER_MIN_CHARS), high)
    )
    if short_override:
        minimum = max(600, int(effective_target * 0.72))
        maximum = max(minimum + 200, int(effective_target * 1.65))
    else:
        # The author-facing product contract is one complete 2200-3000-char
        # chapter.  The profile overlap remains the preferred writing target,
        # but it must not silently weaken the accepted minimum to 1944 or
        # invent a 2968-char internal ceiling that disagrees with the UI.
        minimum = READER_CHAPTER_MIN_CHARS
        maximum = READER_CHAPTER_MAX_CHARS

    return {
        "schema_version": READER_CHAPTER_BUDGET_SCHEMA_VERSION,
        "source": "quality_profile_report_soft_metrics",
        "recommended_range": [low, high],
        "requested_target": requested,
        "target_word_count": effective_target,
        "minimum_chars": minimum,
        "maximum_chars": maximum,
        "short_target_override": short_override,
        "rationale": "完整正文统一验收 2200-3000 字；推荐区间只控制写作落点，完成节拍和章末钩子后立即收束",
    }


_PLATFORM_PROFILES: dict[str, dict[str, Any]] = {
    "fanqie": {
        "label": "番茄式高留存基线",
        "title_rules": ["标题直给核心处境或反差", "优先口语化和可点击的冲突，不堆 SEO 关键词"],
        "synopsis_rules": ["用处境/冲突→能力或机会→下一步悬念的三段压缩结构", "移动端首屏先说人和事，不先科普世界"],
        "opening_rules": ["前 300 字出现具体冲突、异常或强问题", "前三章分别完成身份锚定、核心能力/规则展示和第一次可见反馈"],
        "chapter_rules": ["每章必须有可见状态变化和章末钩子", "前期不允许连续两章只有铺垫而没有情绪或信息兑现"],
        "payoff_policy": {
            "max_low_payoff_streak": 1,
            "early_chapters_need_payoff": 3,
            "hook_required": True,
            "early_min_payoff_intensity": "medium",
            "default_payoff_intensity": "small",
            "feedback_required_types": [
                "status_reversal", "money_or_resource", "opponent_reaction",
                "career_progress", "industry_breakthrough", "system_reward",
            ],
        },
        "attention_beat_rules": ["前 300 字进入具体主题；章内用情绪/信息/行动节点维持追读，不按固定字数硬塞爽点"],
        "reader_priority": "即时冲突、情绪反馈、清晰的下一步期待",
    },
    "qidian": {
        "label": "起点式连载基线",
        "title_rules": ["标题短而有辨识度，保留核心概念或人物状态", "避免短视频式感叹堆叠，形成全书统一标题风格"],
        "synopsis_rules": ["用具体人物困境承载世界格局和长期悬念", "少剧透结果，多给可验证的成长方向"],
        "opening_rules": ["前三章完成主角、核心设定、第一轮小高潮", "重要爽点先铺垫再兑现，结果必须有代价或余波"],
        "chapter_rules": ["每章推进目标、状态或关系，并留下可追读问题", "小冲突递进到阶段高潮，不用每章重复同一种打脸"],
        "payoff_policy": {
            "max_low_payoff_streak": 2,
            "early_chapters_need_payoff": 3,
            "hook_required": True,
            "early_min_payoff_intensity": "medium",
            "default_payoff_intensity": "small",
            "feedback_required_types": [
                "status_reversal", "money_or_resource", "opponent_reaction",
                "career_progress", "industry_breakthrough", "system_reward",
            ],
        },
        "attention_beat_rules": ["前三章完成主角、核心设定和第一轮小高潮；爽点要有铺垫、依据、代价和余波"],
        "reader_priority": "设定可信、成长有代价、伏笔与长期回报",
    },
}


_GENRE_PROFILES: dict[str, dict[str, Any]] = {
    "urban": {
        "label": "都市通用",
        "dialogue_mode": "对白推进目标和关系，叙述落到生活摩擦",
        "ledgers": ["时间线", "资金/债务", "人物关系", "职业或行业事实", "主角能力边界"],
        "payoff_types": ["status_reversal", "money_or_resource", "information_advantage", "relationship_shift", "career_progress"],
        "style_rules": ["用具体场景和行业动作替代空泛成功学", "重要配角有自己的目标、利益和反应", "主角必须主动做选择而不是只接收事件"],
        "anti_ai_rules": ["减少解释式总结和整齐排比", "保留自然口语、停顿和人物差异", "标点按语境使用，只限制整章异常密度"],
    },
    "xuanhuan": {
        "label": "玄幻仙侠通用",
        "dialogue_mode": "战斗、资源和关系选择共同推进，不用设定说明代替戏",
        "ledgers": ["境界与能力边界", "功法/法器/资源消耗", "伤势与恢复", "势力关系", "伏笔与因果", "时间地点"],
        "payoff_types": ["breakthrough", "combat_advantage", "resource_gain", "status_reversal", "reveal", "relationship_shift"],
        "style_rules": ["每次升级写清契机、代价、能力变化和验证", "越阶或逆转必须有设定内依据", "大世界通过事件和人物逐层显露，不整段科普"],
        "anti_ai_rules": ["减少万能境界、战斗过程和围观反应的重复模板", "爽点要有压制、选择、爆发、旁观反应和余波", "不因去 AI 味抹掉必要术语或专有名词"],
    },
}


_SUBGENRE_PROFILES: dict[str, dict[str, Any]] = {
    "urban_shenhao": {
        "label": "都市神豪/商战",
        "opening_rules": ["把现实需求、可用资金/机会和风险同时落到一个具体场景", "金手指必须有规则、冷却或代价"],
        "payoff_types": ["money_or_resource", "status_reversal", "opponent_reaction", "industry_breakthrough"],
        "ledgers": ["资金来源与余额", "项目现金流", "对手与合作方", "合同/风险", "时间节点"],
        "style_rules": ["钱的数字服务选择和节奏，不能只报资产规模", "对手要有资源和合理判断，主角的赢来自眼光、执行或信息差"],
    },
    "urban_business": {
        "label": "都市商战",
        "opening_rules": ["先给订单、裁员、债务、合同或行业规则造成的现实压力"],
        "payoff_types": ["industry_breakthrough", "information_advantage", "status_reversal", "relationship_shift"],
        "ledgers": ["现金流", "项目阶段", "合同责任", "竞争方动作", "行业事实"],
    },
    "urban_rebirth": {
        "label": "都市重生/年代",
        "opening_rules": ["先给具体年代处境和无法回避的选择，再露出信息差", "未来记忆必须有边界，并产生蝴蝶效应"],
        "payoff_types": ["information_advantage", "money_or_resource", "relationship_shift", "status_reversal"],
        "ledgers": ["年月日与年龄", "时代物件/制度", "资金与信息来源", "蝴蝶效应", "家庭关系"],
    },
    "urban_ability": {
        "label": "都市异能",
        "opening_rules": ["能力先在现实场景中解决一个小问题，同时展示限制或副作用"],
        "payoff_types": ["ability_discovery", "combat_advantage", "status_reversal", "reveal"],
        "ledgers": ["能力等级", "冷却/副作用", "普通人认知边界", "现代地点与时间"],
    },
    "urban_high_martial": {
        "label": "都市高武",
        "opening_rules": ["现代生活和异常力量必须同场出现，先让读者感到熟悉与危险的反差"],
        "payoff_types": ["combat_advantage", "ability_discovery", "status_reversal", "reveal"],
        "ledgers": ["现代社会规则", "战力阶梯", "能力代价", "阵营关系", "普通人信息边界"],
    },
    "urban_brainstorm": {
        "label": "都市脑洞",
        "opening_rules": ["用一个可理解的异常规则制造即时问题，不先解释规则全集"],
        "payoff_types": ["rule_exploit", "information_advantage", "status_reversal", "reveal"],
        "style_rules": ["规则每次出现都必须改变选择，不做规则展览"],
    },
    "urban_system": {
        "label": "都市系统",
        "opening_rules": ["系统奖励必须绑定任务、风险或消耗，不能凭空发福利"],
        "payoff_types": ["system_reward", "money_or_resource", "status_reversal", "ability_discovery"],
        "ledgers": ["任务与奖励", "系统限制", "资源余额", "现实后果"],
    },
    "xuanhuan_upgrade": {
        "label": "传统玄幻升级",
        "opening_rules": ["前三章给出明确压制、能力入口和第一次验证，不把升级写成一句状态播报"],
        "payoff_types": ["breakthrough", "combat_advantage", "status_reversal", "resource_gain"],
        "style_rules": ["境界梯度清晰，小境界快、大境界有仪式和代价", "敌人越强，胜利依据越具体"],
    },
    "xuanhuan_mortal": {
        "label": "凡人/苟道",
        "opening_rules": ["先处理生存、资源或信息风险，再暴露底牌", "苟不是停滞，至少要有资源、信息或关系的积累"],
        "payoff_types": ["resource_gain", "information_advantage", "survival", "hidden_strength", "breakthrough"],
        "ledgers": ["资源库存", "风险暴露", "底牌", "人情债", "伤势与恢复"],
    },
    "xuanhuan_cautious": {
        "label": "苟道流",
        "opening_rules": [
            "先处理一个必须马上解决的生存、资源或身份风险，再展示主角的谨慎收益",
            "苟不是停滞，每章至少让资源、信息、关系、身份或风险发生可见变化",
        ],
        "payoff_types": ["survival", "resource_gain", "information_advantage", "hidden_strength", "rule_exploit", "reveal"],
        "style_rules": [
            "主角的谨慎要通过具体选择和后果体现，不能反复用‘小心谨慎’概括",
            "底牌揭示必须改变对手判断或下一步风险，不能只作为角色介绍",
        ],
        "ledgers": ["资源库存", "风险暴露", "底牌", "身份伪装", "人情债"],
    },
    "xuanhuan_epic": {
        "label": "史诗玄幻",
        "opening_rules": ["大谜团必须落在当前人物能感知的具体事件里", "每次扩张世界前先回收或推进一个近景问题"],
        "payoff_types": ["reveal", "status_reversal", "sacrifice", "breakthrough", "faction_shift"],
        "ledgers": ["时代/历史", "势力与战争", "伏笔状态", "关键人物命运", "力量上限"],
    },
    "xuanhuan_destiny": {
        "label": "宿命/仙侠",
        "opening_rules": ["情感执念必须在行动和代价里出现，不用抽象抒情替代选择"],
        "payoff_types": ["relationship_shift", "reveal", "sacrifice", "breakthrough", "reversal"],
        "style_rules": ["意境服务人物和冲突，不能让氛围描写把事件停住"],
    },
    "xuanhuan_setting": {
        "label": "设定/智斗",
        "opening_rules": ["先展示一条规则如何迫使角色选择，再逐步补充规则边界"],
        "payoff_types": ["rule_exploit", "reveal", "information_advantage", "status_reversal"],
        "ledgers": ["规则版本", "信息掌握者", "推理证据", "反转前置线索"],
    },
    "xuanhuan_family": {
        "label": "家族修仙",
        "opening_rules": ["先让家族面对资源/存续压力，人物关系和经营选择同时推进"],
        "payoff_types": ["resource_gain", "faction_shift", "relationship_shift", "breakthrough", "family_survival"],
        "ledgers": ["家族成员与代际", "资源与产业", "盟友/仇敌", "血脉/传承", "家族时间线"],
    },
    "xuanhuan_system": {
        "label": "系统/签到",
        "opening_rules": [
            "奖励频率服务剧情，系统不能替代主角做选择",
            "奖励必须带来新问题、消耗或暴露风险",
            "若金手指是人生模拟器，首轮模拟必须展示从当前状态走到死亡/终局的结果，不能只播报一个未来片段",
        ],
        "payoff_types": ["system_reward", "breakthrough", "resource_gain", "status_reversal"],
        "ledgers": [
            "任务/签到",
            "奖励与限制",
            "模拟分支与终局",
            "模拟收益回收",
            "现实因果偏移",
            "资源余额",
            "能力验证",
            "暴露风险",
        ],
        "style_rules": [
            "模拟未来时先写死亡原因、关键分支和主角的选择，再写收益落地；不能用系统面板替代事件",
            "模拟中获得的机缘、修为、功法或资源可以选择带回现实，但每次回收必须写出取舍、代价和现实后果",
            "模拟结果不是现实事实；只有主角执行选择后才改变现实，选择应推动下一章冲突",
        ],
    },
    "xuanhuan_longlife": {
        "label": "长生流",
        "opening_rules": [
            "先用一个具体的生存、资源或身份问题落地长生设定，不先解释寿元规则全集",
            "长生带来的优势必须同时制造时间、身份或关系上的新麻烦",
        ],
        "payoff_types": ["survival", "resource_gain", "hidden_strength", "status_reversal", "reveal"],
        "ledgers": ["寿元/时间尺度", "身份与年代", "资源库存", "底牌暴露", "人情债"],
        "style_rules": [
            "时间跨度服务剧情和反差，不能用年数台账替代事件",
            "主角的强大通过选择、细节和后果显露，不用旁白反复宣布无敌",
        ],
    },
}


# Optional, genre-scoped style plugin distilled from the user's long-life
# template.  It is deliberately separate from the default xuanhuan profile:
# a normal upgrade or epic story should not inherit its deadpan comedy,
# internalized system and short-sentence targets by accident.
_STYLE_PLUGINS: dict[str, dict[str, Any]] = {
    "xuanhuan_longlife": {
        "label": "长生苟道（反差/种田/系统内化）",
        "compatible_genres": {"xuanhuan"},
        "compatible_subgenres": {
            "xuanhuan_mortal",
            "xuanhuan_cautious",
            "xuanhuan_system",
            "xuanhuan_longlife",
        },
        "directive": [
            "这是可选的长生苟道风格层，只调整叙事节奏、反差和系统呈现，不覆盖本章事实、人物动机或世界规则。",
            "读者爽感优先靠具体处境、选择后果和旁观者误判制造，不靠‘打脸/无敌/逆天’等词语堆砌。",
        ],
        "opening_rules": [
            "首段用一个反差动作或反差判断立住声音：表面平淡/无奈，实际是占到便宜、躲过风险或另有打算；不能只写抽象感慨。",
            "长生、系统或寿元规则先在事件后果中出现，首章不弹出完整规则说明。",
        ],
        "chapter_rules": [
            "主角可以苟，但每章至少推进资源、信息、关系、身份或风险中的一项，苟不是停滞。",
            "节拍表写明的过桥、交易、对抗、修炼或揭示必须写成可见过程，不能用一句旁白从准备跳到结果；首次出现的设定要在动作、对白或代价中给出来源。",
            "规则博弈或谈判必须写出一轮完整动作链：对手施压或展示代价，主角拿出已有物证/规则，对手被迫回应或让步，最后落到可见结果、代价或新压力；不能只写‘凭规则逼退对方’。本章爽点契约中的主动选择、结果和他人反应，至少各落在一处可定位的动作或对白上。",
            "同一装腔翻车、种田日常或吐槽节拍再次出现时，必须更换失败原因、人物反应和具体细节，禁止逐段复刻。",
            "系统奖励尽量内化成时间、资源或能力变化；只有改变选择时才展示提示，不连续弹出面板或数字播报。",
        ],
        "style_rules": [
            "短句白描优先，句长有变化；让动作和对白承担信息，不用整段修炼感悟或旁白总结推进。",
            "反差喜剧要落在人物行动和后果上，吐槽役可以稳定但每次反应要有变量；不要把角色写成只会重复口号的工具人。",
            "跨时代、种田和资源积累要转化为可见场景，少报数字，多写时间改变了什么。",
        ],
        "anti_ai_rules": [
            "不禁用任何标点；只防整章高密度破折号、连续重复句式、模板化收束和成片的工整排比。",
            "段首要有变化：同一个两字人名作为段落开头尽量不超过全章约四分之一；在不丢失第三人称限知的前提下，交替从动作、场景、物件、对白或他人反应起笔，也不要把人名全部粗暴替换成‘他/她’。",
            "同一动作或反应短语（如‘没有说话’‘点了点头’）一章内不要反复充当默认承接；保留必要沉默，但改用视线、手部动作、停顿后的决定、声音变化或环境后果呈现，避免同一短语超过三次。",
            "降低空泛形容词、‘大道/机缘/逆天’式套话和修炼总结；用一个具体动作、物件或误会替代泛泛概括。",
            "重复梗允许保留，但每轮必须有新变量；发现近似段落时优先重写变量和反应，不靠机械删句掩盖。",
        ],
        "attention_beat_rules": [
            "每章用‘小问题解决/新麻烦出现/旁观者误判/底牌露出一角’等事件节点维持追读，不按固定字数硬塞笑点。",
            "章末留下具体的资源、身份、关系或风险变化；不要用‘一切才刚刚开始’这类空钩子。",
        ],
        "ledgers": ["寿元/时间尺度", "身份与年代", "资源库存", "系统任务与限制", "底牌暴露", "人情债"],
        "payoff_types": ["survival", "resource_gain", "hidden_strength", "system_reward", "status_reversal", "relationship_shift"],
        # Soft acceptance targets, never a reason to force unnatural prose.
        "soft_metrics": {
            "mean_sentence_chars_target_max": 22,
            "dialogue_ratio_target": [0.15, 0.20],
            "rhetoric_density_per_1k_max": 4.0,
            "filler_density_per_1k_max": 1.2,
            "payoff_term_density_per_1k_max": 0.5,
            "system_popup_density_per_1k_max": 0.1,
        },
        "provenance": ["系统赋我长生_写作模板"],
    },
}


_SOURCE_PACKS = (
    "都市神豪写作质量包",
    "都市通用写作质量包",
    "玄幻仙侠写作质量包",
    "天蚕土豆写作质量包",
    "网文平台适配质量包",
    "urban-novel-writing-master",
    "xianxia-xuanhuan-writing-master",
    "novel-platform-style-master",
)


def normalize_platform(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return _PLATFORM_ALIASES.get(raw, "fanqie")


def normalize_genre(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in _GENRE_ALIASES:
        return _GENRE_ALIASES[raw]
    if any(token in raw for token in ("都市", "现代", "商战", "重生")):
        return "urban"
    if any(token in raw for token in ("玄幻", "仙侠", "修仙", "高武")):
        return "xuanhuan"
    if any(token in raw for token in ("悬疑", "灵异", "推理")):
        return "suspense"
    if any(token in raw for token in ("科幻", "末日")):
        return "science_fiction"
    if any(token in raw for token in ("游戏", "电竞", "网游")):
        return "game"
    if any(token in raw for token in ("洪荒", "封神")):
        return "fengshen"
    if any(token in raw for token in ("历史", "穿越", "三国", "唐宋", "明清")):
        return "history"
    return "urban" if not raw else raw


def normalize_subgenre(value: Any, genre: str = "") -> str:
    raw = str(value or "").strip().lower()
    if raw in _SUBGENRE_ALIASES:
        return _SUBGENRE_ALIASES[raw]
    for alias, key in _SUBGENRE_ALIASES.items():
        if alias in raw:
            return key
    normalized_genre = normalize_genre(genre)
    if normalized_genre in {"history", "suspense", "science_fiction", "game", "fengshen"}:
        return normalized_genre
    return "xuanhuan_upgrade" if normalized_genre == "xuanhuan" else "urban"


def normalize_style_plugin(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in _STYLE_PLUGIN_ALIASES:
        return _STYLE_PLUGIN_ALIASES[raw]
    for alias, key in _STYLE_PLUGIN_ALIASES.items():
        if alias in raw:
            return key
    return ""


def _unique(items: list[Any]) -> list[str]:
    result: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if value and value not in result:
            result.append(value)
    return result


def select_quality_profile(
    *,
    platform: Any = "",
    genre: Any = "",
    subgenre: Any = "",
    style_plugin: Any = "",
    mechanic_families: list[str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic merged platform + genre + subgenre profile."""
    overrides = overrides if isinstance(overrides, dict) else {}
    platform_key = normalize_platform(overrides.get("platform") or platform)
    genre_key = normalize_genre(overrides.get("genre") or genre)
    subgenre_key = normalize_subgenre(overrides.get("subgenre") or subgenre, genre_key)
    requested_plugin = overrides.get("style_plugin") or overrides.get("writing_plugin") or style_plugin
    requested_families = overrides.get("mechanic_families") or mechanic_families or []
    if not isinstance(requested_families, list):
        requested_families = [requested_families]
    mechanic_keys = _unique(requested_families)
    plugin_key = normalize_style_plugin(requested_plugin)
    plugin_data = _STYLE_PLUGINS.get(plugin_key) if plugin_key else None
    plugin_enabled = bool(
        plugin_data
        and genre_key in (plugin_data.get("compatible_genres") or set())
        and subgenre_key in (plugin_data.get("compatible_subgenres") or set())
    )
    active_plugin = deepcopy(plugin_data) if plugin_enabled else {}
    if requested_plugin and not plugin_data:
        plugin_status = "unknown"
    elif requested_plugin and not plugin_enabled:
        plugin_status = "incompatible"
    else:
        plugin_status = "enabled" if plugin_enabled else "not_requested"
    platform_data = deepcopy(_PLATFORM_PROFILES.get(platform_key, _PLATFORM_PROFILES["fanqie"]))
    genre_data = deepcopy(_GENRE_PROFILES.get(genre_key, _GENRE_PROFILES["urban"]))
    subgenre_data = deepcopy(_SUBGENRE_PROFILES.get(subgenre_key, {}))
    webnovel_strategy = resolve_webnovel_strategy(
        platform=platform_key,
        genre=genre_key,
        subgenre=subgenre_key,
        mechanic_families=mechanic_keys,
        style_plugin=plugin_key,
    )

    merged: dict[str, Any] = {
        "schema_version": QUALITY_PROFILE_SCHEMA_VERSION,
        "profile_id": f"{platform_key}:{genre_key}:{subgenre_key}",
        "platform": platform_key,
        "genre": genre_key,
        "subgenre": subgenre_key,
        # Reality-vs-fiction is an explicit project choice.  The default
        # 爽文 profile allows ordinary real-world place names, while projects
        # that require a fully fictional setting can opt into the hard gate.
        "fictional_setting_required": bool(overrides.get("fictional_setting_required", False)),
        "allow_fictional_violence": bool(
            overrides.get(
                "allow_fictional_violence",
                genre_key in {"xuanhuan", "suspense", "science_fiction", "history", "game", "fengshen"},
            )
        ),
        # Live search is opt-in at the compiled project contract. Existing
        # books remain stable unless the user explicitly enables required
        # research; newly-created books set this field from NovelCreate.
        "web_research_mode": str(overrides.get("web_research_mode") or "off").strip().lower(),
        "label": " / ".join(
            item
            for item in (
                platform_data.get("label"),
                subgenre_data.get("label") or genre_data.get("label"),
                active_plugin.get("label"),
            )
            if item
        ),
        "style_plugin": plugin_key if plugin_enabled else "",
        "style_plugin_status": plugin_status,
        "style_plugin_label": active_plugin.get("label", ""),
        "reader_priority": platform_data.get("reader_priority", ""),
        "title_rules": _unique(platform_data.get("title_rules", []) + subgenre_data.get("title_rules", [])),
        "synopsis_rules": _unique(platform_data.get("synopsis_rules", [])),
        "opening_rules": _unique(
            platform_data.get("opening_rules", [])
            + genre_data.get("opening_rules", [])
            + subgenre_data.get("opening_rules", [])
            + active_plugin.get("opening_rules", [])
        ),
        "chapter_rules": _unique(platform_data.get("chapter_rules", []) + active_plugin.get("chapter_rules", [])),
        "style_rules": _unique(
            genre_data.get("style_rules", [])
            + subgenre_data.get("style_rules", [])
            + active_plugin.get("style_rules", [])
        ),
        "anti_ai_rules": _unique(genre_data.get("anti_ai_rules", []) + active_plugin.get("anti_ai_rules", [])),
        "attention_beat_rules": _unique(
            platform_data.get("attention_beat_rules", [])
            + subgenre_data.get("attention_beat_rules", [])
            + active_plugin.get("attention_beat_rules", [])
        ),
        "dialogue_mode": subgenre_data.get("dialogue_mode") or genre_data.get("dialogue_mode", ""),
        "ledgers": _unique(
            genre_data.get("ledgers", [])
            + subgenre_data.get("ledgers", [])
            + active_plugin.get("ledgers", [])
        ),
        "payoff_types": _unique(
            genre_data.get("payoff_types", [])
            + subgenre_data.get("payoff_types", [])
            + active_plugin.get("payoff_types", [])
        ),
        "style_plugin_directive": _unique(active_plugin.get("directive", [])),
        # Keep the bounded prompt focused on the plugin's highest-leverage
        # controls.  The full merged lists remain available to later stages,
        # while generation sees the opening rule, visible-beat rule, and the
        # negotiation/payoff rule before the generic profile queue can crowd
        # them out.
        "style_plugin_rules": _unique(
            list(active_plugin.get("directive", [])[:1])
            + list(active_plugin.get("opening_rules", [])[:1])
            + list(active_plugin.get("chapter_rules", [])[:3])
            + list(active_plugin.get("anti_ai_rules", [])[:3])
            + list(active_plugin.get("attention_beat_rules", [])[:1])
        ),
        "style_plugin_soft_metrics": deepcopy(active_plugin.get("soft_metrics") or {}),
        "mechanic_families": mechanic_keys,
        "quality_strategy": webnovel_strategy,
        "payoff_policy": deepcopy(platform_data.get("payoff_policy", {})),
        "payoff_strategy": select_payoff_strategy(platform_key, genre_key, subgenre_key),
        # Product-wide narrative contract.  It is intentionally not an
        # author-style override: all current web-novel profiles use third
        # person in narration, while quoted character voice may still use
        # first person.
        "narrative_pov": THIRD_PERSON_NARRATIVE_POLICY,
        "provenance": _unique(list(_SOURCE_PACKS) + active_plugin.get("provenance", [])),
    }
    strategy = merged["payoff_strategy"]
    merged["payoff_policy"].update({
        "max_low_payoff_streak": strategy.get("max_low_payoff_streak"),
        "early_chapters_need_payoff": strategy.get("early_chapter_count"),
        "early_min_payoff_intensity": strategy.get("early_min_intensity"),
        "default_payoff_intensity": strategy.get("default_intensity"),
        "feedback_required": bool(strategy.get("feedback_required")),
        "payoff_type_cycle": list(strategy.get("type_cycle") or []),
        "no_repeat_window": int(strategy.get("no_repeat_window") or 0),
    })
    merged["failure_pattern_constraints"] = generation_constraints(profile=merged)
    if merged["web_research_mode"] not in {"off", "required"}:
        raise ValueError("web_research_mode must be off or required")
    # Project-level explicit settings are additive, never a replacement for
    # the built-in safety contract.
    for key in ("opening_rules", "chapter_rules", "style_rules", "anti_ai_rules", "ledgers", "payoff_types", "attention_beat_rules"):
        if isinstance(overrides.get(key), list):
            merged[key] = _unique(merged[key] + overrides[key])
    if isinstance(overrides.get("payoff_policy"), dict):
        merged["payoff_policy"].update({
            k: v
            for k, v in overrides["payoff_policy"].items()
            if k in {
                "max_low_payoff_streak",
                "early_chapters_need_payoff",
                "hook_required",
                "early_min_payoff_intensity",
                "default_payoff_intensity",
                "feedback_required_types",
            }
        })
    if isinstance(overrides.get("payoff_strategy"), dict):
        # Project-level strategy overrides can tune the curve, but cannot
        # remove the safety/continuity contract or replace the selected
        # platform/genre strategy wholesale.
        merged["payoff_strategy"].update({
            key: value
            for key, value in overrides["payoff_strategy"].items()
            if key in {
                "label", "default_intensity", "early_min_intensity",
                "early_chapter_count", "max_low_payoff_streak",
                "feedback_required", "no_repeat_window", "type_cycle",
                "directive",
            }
        })
        strategy = merged["payoff_strategy"]
        merged["payoff_policy"].update({
            "max_low_payoff_streak": strategy.get("max_low_payoff_streak"),
            "early_chapters_need_payoff": strategy.get("early_chapter_count"),
            "early_min_payoff_intensity": strategy.get("early_min_intensity"),
            "default_payoff_intensity": strategy.get("default_intensity"),
            "feedback_required": bool(strategy.get("feedback_required")),
            "payoff_type_cycle": list(strategy.get("type_cycle") or []),
            "no_repeat_window": int(strategy.get("no_repeat_window") or 0),
        })
    merged["failure_pattern_constraints"] = generation_constraints(profile=merged)
    merged["attention_beat_policy"] = {
        "soft": True,
        "description": "以读者注意力节点为软基线，不按固定字数硬切段或硬塞爽点",
        "max_low_payoff_streak": merged["payoff_policy"].get("max_low_payoff_streak", 2),
        "payoff_phases": ["pressure", "build", "burst", "feedback", "aftershock"],
    }
    report_pack = select_report_pack(
        platform=platform_key,
        genre=genre_key,
        subgenre=subgenre_key,
    )
    merged["report_pack"] = report_pack
    merged["report_pack_id"] = report_pack.get("pack_id")
    merged["report_hard_contracts"] = list(report_pack.get("hard_contracts") or [])
    merged["report_soft_metrics"] = deepcopy(report_pack.get("soft_metrics") or {})
    merged["report_directive"] = render_report_directive(report_pack)
    merged["report_validator_targets"] = list(report_pack.get("validator_targets") or [])
    merged["report_provenance"] = {
        "source": report_pack.get("source_id"),
        "refs": list(report_pack.get("report_refs") or []),
        "hard_gate": False,
    }
    merged["ledgers"] = _unique(
        list(merged.get("ledgers") or []) + list(report_pack.get("ledger_rules") or [])
    )
    merged["chapter_rules"] = _unique(
        list(merged.get("chapter_rules") or []) + list(report_pack.get("hard_contracts") or [])
    )
    merged["provenance"] = _unique(
        list(merged.get("provenance") or []) + [str(report_pack.get("source_id") or "")]
    )
    return merged


def profile_from_context(context: dict[str, Any] | None) -> dict[str, Any]:
    context = context if isinstance(context, dict) else {}
    raw = dict(context.get("quality_profile") or {}) if isinstance(context.get("quality_profile"), dict) else {}
    # Explicit project fields are authoritative. A stored profile is a compiled
    # snapshot and may be stale after a plan rewrite; its old subgenre/profile
    # id must not silently change the active writing strategy.
    for key in (
        "platform",
        "genre",
        "subgenre",
        "style_plugin",
        "writing_plugin",
        "fictional_setting_required",
        "allow_fictional_violence",
        "web_research_mode",
    ):
        value = context.get(key)
        if value not in (None, ""):
            raw[key] = value
    mechanic_families = raw.get("mechanic_families")
    if not isinstance(mechanic_families, list) or not mechanic_families:
        core_contract = context.get("core_mechanic_contract")
        if isinstance(core_contract, dict):
            declared = core_contract.get("mechanic_families") or core_contract.get("mechanic_type")
            mechanic_families = declared if isinstance(declared, list) else [declared] if declared else []
    if not mechanic_families:
        # Import locally to keep planning_contract and quality_profiles free of
        # a module-level cycle while still deriving a strategy from the user's
        # original idea when no compiled contract exists yet.
        from .planning_contract import mechanic_families_for_idea

        idea = " ".join(
            str(context.get(key) or "")
            for key in ("idea", "inspiration", "idea_expanded", "core_hook")
        )
        mechanic_families = mechanic_families_for_idea(idea)
    raw["mechanic_families"] = _unique(mechanic_families or [])
    return select_quality_profile(
        platform=context.get("platform") or context.get("platform_key") or context.get("publish_platform"),
        genre=context.get("genre") or context.get("category") or context.get("main_category"),
        subgenre=context.get("subgenre") or context.get("sub_category") or context.get("theme"),
        style_plugin=context.get("style_plugin") or context.get("writing_plugin"),
        mechanic_families=raw.get("mechanic_families") or [],
        overrides=raw if isinstance(raw, dict) else None,
    )


def compile_quality_directive(
    profile: dict[str, Any] | None,
    *,
    chapter_number: int | None = None,
    chapter_function: dict[str, Any] | None = None,
    payoff_contract: dict[str, Any] | None = None,
    active_rules: list[Any] | None = None,
    opening_plan: dict[str, Any] | None = None,
    readability_plan: dict[str, Any] | None = None,
) -> str:
    """Compile only the relevant rules into a bounded Writer directive.

    P2-2 质量整改：精简为核心5条，避免prompt过载让模型分心。
    详细方法论移到生成后审查逻辑中，生成前只给核心约束。
    核心5条：开局模式、爽点执行、反馈要求、去AI味底线、节奏控制。
    """
    profile = profile if isinstance(profile, dict) else select_quality_profile()
    seq = int(chapter_number or 1)
    strategy = profile.get("quality_strategy") or {}
    payoff_strategy = profile.get("payoff_strategy") or {}
    payoff_policy = profile.get("payoff_policy") or {}

    # 1. 开局模式
    opening = strategy.get("opening") or {}
    opening_mode = opening.get("mode") or "fast_hook"
    opening_directive = opening.get("directive") or "前300字必须落到具体处境/压力，不要铺垫背景"
    if isinstance(opening_plan, dict) and opening_plan.get("mode"):
        opening_mode = str(opening_plan.get("label") or opening_plan.get("mode"))
        opening_directive = str(opening_plan.get("directive") or opening_directive)
        recent_modes = "、".join(str(item) for item in (opening_plan.get("forbidden_recent_modes") or [])) or "无"
        opening_text = (
            f"开场执行模式：{opening_mode}——{opening_directive}；"
            f"最近三章已用类型：{recent_modes}，本章不得重复；"
            "身体感受不是默认开场，只有伤势是本场核心时才使用。"
        )
    else:
        opening_text = (
            f"开局模式：{opening_mode}——{opening_directive}；"
            "身体感受不是默认开场，只有伤势是本场核心时才使用。"
        )
    if seq <= 3:
        opening_text += "；开篇阶段必须尽快落到具体处境/冲突，完成可见反馈，并把下一章问题落到动作或发现。"

    # 2. 爽点执行
    chapter_mode = str(
        (chapter_function or {}).get("chapter_type")
        or (chapter_function or {}).get("chapter_mode")
        or "normal"
    ).strip().lower()
    mode_policy = (payoff_strategy.get("chapter_modes") or {}).get(chapter_mode) or {}
    active_choice_required = bool(mode_policy.get("active_choice_required", True))
    payoff_floor = str(
        payoff_policy.get("early_min_payoff_intensity")
        if seq <= int(payoff_policy.get("early_chapters_need_payoff") or 0)
        else payoff_policy.get("default_payoff_intensity") or "small"
    )
    payoff_streak = int(payoff_policy.get("max_low_payoff_streak") or 1)
    action_rule = (
        "主角必须主动选择造成一处可见变化"
        if active_choice_required
        else "可以承接前章后果，但必须兑现前章后果或制造新的可见压力"
    )
    payoff_text = (
        f"爽点策略/执行：{action_rule}；按压制→蓄力→爆发→反馈→余波推进；"
        f"当前阶段爽点强度不低于{payoff_floor}档，不能连续超过{payoff_streak}章只有铺垫没有兑现。"
    )

    # 3. 反馈要求
    payoff = strategy.get("payoff") or {}
    feedback_channels = payoff.get("feedback_channels") or []
    feedback_text = (
        "反馈要求：反馈必须落到对手/组织/资源/规则/旁观者的可见变化，"
        "不得固定写成'众人震惊'，但也不能只在旁白里宣布爽；"
        f"反馈渠道轮换：{'、'.join(str(item) for item in feedback_channels[:4]) or '多渠道交替'}，同一渠道不得连续模板化复用。"
    )

    # 4. 去AI味底线
    deai = strategy.get("deai") or {}
    deai_pre = deai.get("pre_generation") or []
    deai_text = (
        "去AI味底线：避免同构句、模板套话、总结体，用动作/对白/人物口吻承载情绪；"
        "段首承接要有变化，同一两字人名作为段落开头尽量不超过全章约四分之一；"
        "标点不设禁用清单，只处理整章高密度、连续重复或模板化使用。"
    )
    if deai_pre:
        deai_text += "；重点注意：" + "；".join(str(item) for item in deai_pre[:2])

    # 5. 节奏控制
    chapter_rules = profile.get("chapter_rules") or []
    rhythm_text = (
        "节奏控制：每约800-1200字出现一次局部变化（动作/信息/情绪转折）；"
        "转折前给读者可见的动作、线索或异常，高潮后留下具体余波；"
        "节拍表写明的过桥、交易、对抗、修炼或揭示必须写成可见过程，不能用一句旁白从准备跳到结果；"
        "人物只能使用自己已经获得的信息，能力/物品/时间/地点必须有来源。"
    )
    if chapter_rules:
        rhythm_text += "；" + "；".join(str(item) for item in chapter_rules[:2])

    # 差异化策略核心（保留题材/平台特色）
    market_directive = market_benchmark_directive(strategy.get("market_benchmark"))
    style_plugin = profile.get("style_plugin_directive") or []
    style_text = ""
    if market_directive or style_plugin:
        style_parts = []
        if market_directive:
            style_parts.append(f"平台/题材实证软基线：{market_directive}")
        if style_plugin:
            style_parts.append(
                "可选风格插件（已启用）："
                + "；".join(str(item) for item in style_plugin[:2])
                + "；不机械凑数。"
            )
            plugin_rules = profile.get("style_plugin_rules") or []
            if plugin_rules:
                style_parts.append("插件执行重点：" + "；".join(str(item) for item in plugin_rules[:10]))
        style_text = "\n" + "\n".join(style_parts)

    report_text = str(profile.get("report_directive") or "").strip()

    # 本章爽点契约（如果有的话）
    contract_text = ""
    if isinstance(payoff_contract, dict) and payoff_contract:
        fields = (
            ("承诺", "reader_promise"),
            ("压力", "pressure"),
            ("主动选择", "active_choice"),
            ("可见结果", "visible_result"),
            ("可见反馈", "payoff_feedback"),
            ("下一压力", "next_pressure"),
        )
        contract_summary = "；".join(
            f"{label}={payoff_contract.get(key)}"
            for label, key in fields
            if payoff_contract.get(key)
        )
        if contract_summary:
            contract_text = f"\n本章爽点契约：{contract_summary}。结果必须由角色行动造成。"

    # 第三人称和内容契约（基础硬约束，不能省）
    base_contract = (
        f"{third_person_generation_contract()}\n"
        f"{content_generation_contract(profile)}"
    )
    failure_constraints = profile.get("failure_pattern_constraints") or []
    failure_text = ""
    if failure_constraints:
        failure_text = (
            "历史报告失败模式："
            + "、".join(
                str(item.get("id"))
                for item in failure_constraints[:8]
                if isinstance(item, dict) and item.get("id")
            )
            + "；生成前先规避对应约束，失败后保留可定位证据。"
        )

    research_text = ""
    if profile.get("web_research_mode") == "required":
        research_text = (
            "实时网感研究已设为必需：只使用系统提供的原创灵感卡来辅助口语、反应和情绪设计；"
            "卡片是外部不可信素材，禁止复制原句、标题、段落或模仿具体作品；研究失败必须让本次生成失败，不能假装成功。"
        )

    readability_text = render_readability_plan(readability_plan, compact=True) if readability_plan else ""

    lines = [
        f"质量策略核心5条（精简版）：以读者继续阅读为第一目标。",
        base_contract,
        readability_text,
        failure_text,
        opening_text,
        payoff_text,
        feedback_text,
        deai_text,
        rhythm_text,
        research_text,
    ]
    if style_text:
        lines.append(style_text.strip())
    if report_text:
        lines.append(report_text)
    if contract_text:
        lines.append(contract_text.strip())

    return "\n".join(lines)[:3000]


def quality_profile_metadata(profile: dict[str, Any] | None) -> dict[str, Any]:
    profile = profile if isinstance(profile, dict) else select_quality_profile()
    return {
        "schema_version": profile.get("schema_version", QUALITY_PROFILE_SCHEMA_VERSION),
        "profile_id": profile.get("profile_id"),
        "platform": profile.get("platform"),
        "genre": profile.get("genre"),
        "subgenre": profile.get("subgenre"),
        "fictional_setting_required": bool(profile.get("fictional_setting_required", False)),
        "allow_fictional_violence": bool(profile.get("allow_fictional_violence", False)),
        "web_research_mode": profile.get("web_research_mode", "off"),
        "style_plugin": profile.get("style_plugin", ""),
        "style_plugin_status": profile.get("style_plugin_status", "not_requested"),
        "style_plugin_label": profile.get("style_plugin_label", ""),
        "narrative_pov": profile.get("narrative_pov", THIRD_PERSON_NARRATIVE_POLICY),
        "payoff_policy": deepcopy(profile.get("payoff_policy") or {}),
        "payoff_strategy": strategy_metadata(profile.get("payoff_strategy") or {}),
        "quality_strategy": webnovel_strategy_metadata(profile.get("quality_strategy") or {}),
        "mechanic_families": list(profile.get("mechanic_families") or []),
        "failure_patterns": failure_pattern_metadata(
            pattern_ids=[
                str(item.get("id"))
                for item in (profile.get("failure_pattern_constraints") or [])
                if isinstance(item, dict) and item.get("id")
            ]
        ),
        "ledgers": list(profile.get("ledgers") or []),
        "style_plugin_soft_metrics": deepcopy(profile.get("style_plugin_soft_metrics") or {}),
        "report_pack": report_pack_metadata(profile.get("report_pack")),
        "report_pack_id": profile.get("report_pack_id", ""),
        "report_hard_contracts": list(profile.get("report_hard_contracts") or []),
        "report_soft_metrics": deepcopy(profile.get("report_soft_metrics") or {}),
        "reader_chapter_budget": reader_chapter_budget(profile),
        "report_validator_targets": list(profile.get("report_validator_targets") or []),
        "report_provenance": deepcopy(profile.get("report_provenance") or {}),
        "provenance": list(profile.get("provenance") or []),
        "knowledge_provenance": knowledge_source_metadata(),
    }
