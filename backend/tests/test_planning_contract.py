from app.services.planning_contract import (
    creative_bible_section_defects,
    mechanic_contract_guidance,
    mechanic_families_for_idea,
    validate_core_mechanic_contract,
    validate_longform_contract,
    validate_simulator_contract,
    validate_chapter_outline_user_contract,
    validate_volume_plan_contract,
)


def _core_mechanic_contract() -> dict:
    return {
        "enabled": True,
        "mechanic_type": "simulator",
        "reader_promise": "提前看见危险并用选择换取活路",
        "trigger_and_loop": "触发→选择→行动→收益→代价→状态变化→新冲突",
        "capability_loop": "触发→选择→行动→可见收益→代价→状态变化→新问题",
        "choice_surface": "选择、取舍、放弃或承担风险",
        "visible_payoff": "事件和对手反应中的收益，对手当场失去先手，旁观者震惊",
        "limits_and_costs": "次数、资源、冷却和因果代价",
        "failure_and_risks": "失败、暴露和反噬",
        "state_writeback": "写回现实状态并产生后果",
        "plot_coupling": "推动主线冲突并制造新问题",
        "progression": "分阶段升级能力",
        "anti_inflation": "不能替主角通关，强收益带来新债务",
        "mechanic_specific_contract": "模拟器每次展示终局分支，主角选择回收并承担因果偏移",
    }


def _longform_contract(target: int = 1_500_000) -> dict:
    return {
        "target_words": target,
        "volume_count": 2,
        "volume_word_targets": [750_000, 750_000],
        "chapter_word_target": 3000,
        "chapter_count": 500,
        "route_milestones": [
            {"label": "前期", "start_words": 0, "end_words": 750_000, "goal": "建立冲突"},
            {"label": "后期", "start_words": 750_000, "end_words": target, "goal": "完成主线"},
        ],
    }


def _bible(target: int = 1_500_000) -> str:
    return (
        f"核心设定。黄金三章。能力边界与代价。长篇路线：阶段一 0-75万字，阶段二 75-150万字。"
        f"篇幅与内容配比：目标总字数：{target}字。人物关系。持续校验清单。"
        "爽点阶梯。反馈轮换。金手指创新路径。"
        + "具体执行规则。" * 500
    )


def test_longform_contract_rejects_route_beyond_target():
    output = {
        "creative_bible": _bible(),
        "longform_contract": {
            **_longform_contract(),
            "route_milestones": [
                {"label": "前期", "start_words": 0, "end_words": 1_500_000, "goal": "建立冲突"},
                {"label": "错误路线", "start_words": 1_500_000, "end_words": 6_000_000, "goal": "越界"},
            ],
        },
    }
    defects = validate_longform_contract(output, idea="玄幻长篇", target_words=1_500_000)
    assert any("不能超过" in item or "超过项目目标" in item for item in defects)


def test_explicit_first_chapter_promise_cannot_be_deferred_to_chapter_two():
    idea = (
        "第一章就是军事行动，装甲车撞开午门，三分钟控制紫禁城，"
        "慈禧被架起来时还在想他们为什么不行礼。"
    )
    output = {
        "chapter_outlines": [
            {
                "seq": 1,
                "outline": "林辰在实验室确认时空锚点，决定调动部队控制紫禁城。",
                "beats": ["发现锚点", "测试规则", "制定行动计划"],
                "chapter_goal": "为军事行动做准备",
                "delivery_state": "planned_for_later",
            },
            {
                "seq": 2,
                "outline": "装甲车撞开午门，部队控制紫禁城并抓住慈禧。",
                "beats": ["撞开午门", "控制紫禁城", "抓住慈禧"],
                "chapter_goal": "完成军事行动",
            },
        ],
    }

    defects = validate_chapter_outline_user_contract(output, idea=idea)

    assert any("第 1 章" in item and "不得延后" in item for item in defects)


def test_explicit_first_chapter_promise_passes_when_result_is_visible_in_chapter_one():
    idea = (
        "第一章就是军事行动，装甲车撞开午门，三分钟控制紫禁城，"
        "慈禧被架起来时还在想他们为什么不行礼。"
    )
    output = {
        "chapter_outlines": [
            {
                "seq": 1,
                "outline": "装甲车撞开午门，部队三分钟控制紫禁城，慈禧被士兵架起仍质问为何不行礼。",
                "beats": ["装甲车撞开午门", "三分钟控制紫禁城", "架起慈禧"],
                "chapter_goal": "当章完成军事行动",
                "explicit_user_contract": idea,
                "must_deliver": ["撞开午门", "控制紫禁城", "架起慈禧"],
                "delivery_state": "completed_in_chapter",
                "visible_result": "紫禁城被控制，慈禧被架起",
            }
        ],
    }

    assert validate_chapter_outline_user_contract(output, idea=idea) == []


def test_longform_parser_accepts_shorthand_total_without_trailing_character():
    output = {
        "creative_bible": _bible().replace(
            "目标总字数：1500000字", "目标总字数150万；750章，每章2000字"
        ),
        "longform_contract": _longform_contract(),
    }
    defects = validate_longform_contract(output, idea="玄幻长篇", target_words=1_500_000)
    assert not any("不一致的总字数" in item for item in defects)


def test_simulator_contract_requires_terminal_future_and_harvest_choice():
    defects = validate_simulator_contract(
        {
            "enabled": True,
            "horizon": "模拟未来三天",
            "terminal_condition": "看到一次危险",
            "branches": ["一条"],
            "observable_state": ["状态"],
            "harvestable_rewards": ["金币"],
            "selection_rules": ["选择"],
            "costs_and_risks": "消耗一次机会",
            "reality_writeback": "写回现实",
        },
        required=True,
    )
    assert any("死亡或终局" in item for item in defects)
    assert any("机缘、修为、功法" in item for item in defects)


def test_simulator_contract_does_not_reject_a_negated_no_full_harvest_rule():
    contract = {
        "enabled": True,
        "horizon": "从当前推演到死亡终局",
        "terminal_condition": "死亡或寿终",
        "branches": ["保守路线", "激进路线"],
        "observable_state": ["修为", "资源", "死亡原因"],
        "harvestable_rewards": ["机缘", "修为", "功法", "资源", "能力"],
        "selection_rules": "主角可以选择、组合或放弃收益，不能无条件全拿",
        "costs_and_risks": "次数、寿元、资源、冷却、因果和暴露代价",
        "reality_writeback": "选择回收后写回现实并改变后续冲突",
        "causal_recalculation": "回收后重新推演因果分支",
        "plot_guardrails": "收益不能跳过主线，必须带来代价或新问题",
    }
    defects = validate_simulator_contract(contract, required=True)
    assert not any("无条件全量带回" in item for item in defects)


def test_core_mechanic_contract_is_generic_and_reusable():
    assert validate_core_mechanic_contract(_core_mechanic_contract(), required=True) == []
    defects = validate_core_mechanic_contract(
        {"enabled": True, "mechanic_type": "space", "reader_promise": "囤货"},
        required=True,
    )
    assert any("能力循环" in item for item in defects)


def test_mechanic_adapters_cover_multiple_families_and_combinations():
    assert mechanic_families_for_idea("重生后绑定系统，储物空间里还有灵泉") == [
        "system", "rebirth", "space"
    ]
    guidance = mechanic_contract_guidance("重生后绑定系统，储物空间里还有灵泉")
    assert "系统/签到/任务" in guidance
    assert "重生/回到过去" in guidance
    assert "空间/灵泉/储物" in guidance


def test_plain_worldbuilding_space_does_not_trigger_a_cheat_adapter():
    assert mechanic_families_for_idea("人类在深空建立殖民地，空间站之间爆发战争") == []


def test_non_simulator_mechanic_requires_type_specific_rules_without_forcing_simulator():
    contract = _core_mechanic_contract()
    contract.update({
        "mechanic_type": "rebirth",
        "mechanic_specific_contract": "保留前世记忆，主动改写关键事件；蝴蝶效应会造成误差和暴露",
        "capability_loop": "触发记忆→选择改写→行动布局→收益→代价→状态变化→新冲突",
        "limits_and_costs": "未来信息会因改写产生误差，关键节点可能暴露并付出关系代价",
    })
    assert validate_core_mechanic_contract(contract, required=True) == []


def test_unknown_named_cheat_falls_back_to_generic_adapter():
    contract = _core_mechanic_contract()
    contract.update({
        "mechanic_type": "观测之眼",
        "mechanic_specific_contract": "只能观测已发生的线索，使用后会暴露观察痕迹并引来追查",
    })
    assert validate_core_mechanic_contract(contract, required=True) == []


def test_explicit_unnamed_cheat_uses_generic_ability_adapter():
    assert mechanic_families_for_idea("主角获得一个未知金手指，规则随剧情逐步揭开") == ["ability"]


def test_complete_longform_contract_is_accepted():
    output = {
        "creative_bible": _bible(),
        "longform_contract": _longform_contract(),
        "core_mechanic_contract": {"enabled": False},
        "simulator_contract": {"enabled": False},
    }
    assert validate_longform_contract(
        output,
        idea="玄幻长篇",
        target_words=1_500_000,
    ) == []


def test_creative_bible_requires_all_operational_sections():
    defects = creative_bible_section_defects("黄金三章。人物关系。")
    assert any("能力边界" in item for item in defects)
    assert any("篇幅与内容配比" in item for item in defects)
    assert any("持续校验" in item for item in defects)


def test_volume_plan_requires_exact_word_ledger_and_contiguous_chapters():
    output = {
        "total_word_target": 1_500_000,
        "volumes": [
            {"start_chapter": 1, "end_chapter": 100, "word_target": 700_000},
            {"start_chapter": 102, "end_chapter": 200, "word_target": 700_000},
        ],
    }
    defects = validate_volume_plan_contract(output, target_words=1_500_000)
    assert any("合计" in item for item in defects)
    assert any("连续" in item for item in defects)


def test_volume_plan_requires_declared_total_word_target():
    defects = validate_volume_plan_contract(
        {
            "volumes": [
                {"start_chapter": 1, "end_chapter": 100, "word_target": 1_500_000},
            ],
        },
        target_words=1_500_000,
    )
    assert any("total_word_target" in item for item in defects)
