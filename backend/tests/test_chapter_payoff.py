from app.services.chapter_payoff import (
    build_payoff_contract,
    evaluate_payoff_schedule,
    normalize_payoff_contract,
    repair_payoff_beat_structure,
    score_payoff_contract,
    validate_payoff_beat_structure,
    validate_payoff_contract,
    validate_payoff_evidence,
)
from app.services.quality_profiles import select_quality_profile


def _contract(chapter_number: int = 1) -> dict:
    return {
        "chapter_number": chapter_number,
        "reader_promise": "等主角在众人面前证明自己",
        "pressure": "公开测试失败会被逐出队伍",
        "active_choice": "主角主动要求再测一次",
        "payoff_type": "status_reversal",
        "visible_result": "石碑显示出新的数值",
        "witness_reaction": "嘲笑声停了",
        "cost": "能力暴露出冷却限制",
        "next_pressure": "长老要求第二天复核",
        "setup_refs": ["前三章的旧玉佩"],
    }


def test_payoff_contract_requires_choice_result_and_next_pressure():
    profile = select_quality_profile(genre="玄幻", subgenre="传统升级流")
    result = validate_payoff_contract(_contract(), profile=profile, required=True)

    assert result["passed"] is True
    assert result["missing"] == []

    incomplete = validate_payoff_contract(
        {"chapter_number": 1, "reader_promise": "有事发生"},
        profile=profile,
        required=True,
    )
    assert incomplete["passed"] is False
    assert "active_choice" in incomplete["missing"]


def test_payoff_evidence_must_be_locatable_in_actual_text():
    text = "石碑显示出新的数值。台下的嘲笑声停了。"
    good = validate_payoff_evidence(
        text,
        [{"type": "status_reversal", "anchor": "石碑显示出新的数值", "result": "数值发生变化"}],
        required=True,
    )
    bad = validate_payoff_evidence(
        text,
        [{"type": "status_reversal", "anchor": "主角击败了长老", "result": "赢了"}],
        required=True,
    )

    assert good["passed"] is True
    assert bad["passed"] is False

    partial = validate_payoff_evidence(
        text,
        [
            {"type": "status_reversal", "anchor": "石碑显示出新的数值", "result": "数值发生变化"},
            {"type": "status_reversal", "anchor": "主角击败了长老", "result": "赢了"},
        ],
        required=True,
    )
    assert partial["passed"] is True
    assert len(partial["invalid"]) == 1

    punctuation_only_difference = validate_payoff_evidence(
        "她说：“我宣布，从今天起，公司进入内部审计程序。所有支出重新核查。”",
        [{
            "type": "status_reversal",
            "anchor": "“我宣布，从今天起，公司进入内部审计程序。”",
            "result": "主角宣布审计",
        }],
        required=True,
    )
    assert punctuation_only_difference["passed"] is True
    assert punctuation_only_difference["checked"][0]["match_mode"] == "punctuation_normalized"

    # The final humanizer may change a connective while preserving the actual
    # scene evidence. A long contiguous run from the final text is still
    # verifiable; a short keyword overlap is not enough for this path.
    rewritten = (
        "我按下播放键。听筒里传来的声音，跟刚才那段语音不一样。"
        "背景音里没有脚步声，没有金属摩擦，只有风声，很大的风声，"
        "像是在一个空旷的地方录的。林峰的声音很轻，像是用最后一点力气说出来的："
        "‘别进来。他们知道你会来。’"
    )
    rewritten_anchor = (
        "我按下播放键。听筒里传来的声音，比刚才那段语音不一样。"
        "背景音里没有脚步声，没有金属摩擦，只有风声，很大的风声，"
        "像是在一个空旷的地方录的。林峰的声音很轻，像是用最后一点力气说出来的："
        "“别进来。他们知道你会来。”"
    )
    rewritten_result = validate_payoff_evidence(
        rewritten,
        [{"type": "reveal", "anchor": rewritten_anchor, "result": "收到关键警告"}],
        required=True,
    )
    assert rewritten_result["passed"] is True
    assert rewritten_result["checked"][0]["match_mode"] == "fuzzy_contiguous"
    assert rewritten_result["checked"][0]["anchor"] in rewritten


def test_payoff_score_accepts_natural_observable_result_paraphrase():
    profile = select_quality_profile(genre="玄幻", subgenre="传统升级流")
    contract = build_payoff_contract(
        {
            **_contract(),
            "visible_result": "发现纸条，得知‘借期已至’",
            "payoff_type": "reveal",
        },
        chapter_number=1,
        profile=profile,
    )
    scored = score_payoff_contract(
        contract,
        profile=profile,
        text="他余光瞥见门缝下多了一样东西。一张纸。展开后，纸上只写着：‘借期已至’。",
    )
    assert scored["evidence"]["visible_result"] is True
    assert scored["dimensions"]["result_visibility"] == 100


def test_provider_facing_chinese_payoff_labels_map_to_canonical_types():
    contract = normalize_payoff_contract({"chapter_number": 1, "payoff_type": "身份反转"})
    assert contract["payoff_type"] == "status_reversal"


def test_explicit_other_payoff_is_valid_when_reader_contract_is_complete():
    profile = select_quality_profile(genre="都市")
    contract = {
        "chapter_number": 1,
        "reader_promise": "主角必须在今晚做出选择",
        "pressure": "错过窗口就会失去唯一机会",
        "active_choice": "主角主动放弃安全方案，选择进入现场",
        "payoff_type": "other",
        "visible_result": "现场留下新的证据，原本的计划被迫改变",
        "next_pressure": "对方已经知道他介入了这件事",
    }

    result = validate_payoff_contract(contract, profile=profile, required=True)

    assert result["passed"] is True
    assert result["missing"] == []

    contract = normalize_payoff_contract({
        "chapter_number": 2,
        "payoff_type": "other",
        "active_choice": "主角选择当场解雇财务总监",
        "visible_result": "财务总监被保安带走，员工重新评估主角",
    })
    assert contract["payoff_type"] == "status_reversal"

    contract = normalize_payoff_contract({
        "chapter_number": 1,
        "payoff_type": "other",
        "reader_promise": "主角用年终奖买下公司，实现身份逆转",
        "visible_result": "主角成为公司最大股东，原CEO被解职",
    })
    assert contract["payoff_type"] == "status_reversal"

    contract = normalize_payoff_contract({
        "chapter_number": 3,
        "payoff_type": "other",
        "payoff_evidence": [{"type": "权力确立", "result": "对手被迫退场"}],
    })
    assert contract["payoff_type"] == "status_reversal"

    contract = normalize_payoff_contract({"chapter_number": 1, "type": "突破"})
    assert contract["payoff_type"] == "breakthrough"

    contract = normalize_payoff_contract({"chapter_number": 1, "kind": "资源获取"})
    assert contract["payoff_type"] == "resource_gain"

    contract = normalize_payoff_contract({
        "chapter_number": 1,
        "payoff_type": "other",
        "reader_promise": "完成身份反转",
        "visible_result": "主角成为公司新老板",
    })
    assert contract["payoff_type"] == "status_reversal"

    contract = normalize_payoff_contract({
        "chapter_number": 2,
        "payoff_type": "other",
        "reader_promise": "主角在会议上展现掌控力，并给出初步应对方案",
        "visible_result": "会议通过主角的提案，部分高管态度转变",
        "witness_reaction": "财务总监和销售总监从怀疑到配合",
        "payoff_evidence": [{"type": "能力展示", "result": "主角提出明确决策"}],
    })
    assert contract["payoff_type"] == "status_reversal"


def test_low_payoff_schedule_is_a_soft_reader_gate_not_a_word_count_gate():
    profile = select_quality_profile(platform="番茄", genre="都市", subgenre="都市神豪")
    chapters = [
        {"payoff_contract": build_payoff_contract(_contract(i), chapter_number=i, profile=profile)}
        for i in range(1, 4)
    ]
    chapters[1]["payoff_contract"]["visible_result"] = ""
    chapters[1]["payoff_contract"]["payoff_type"] = ""

    result = evaluate_payoff_schedule(chapters, profile=profile)
    assert result["passed"] is True
    assert result["max_low_payoff_streak"] == 1


def test_payoff_strength_requires_visible_feedback_for_public_high_payoffs():
    profile = select_quality_profile(platform="番茄", genre="都市", subgenre="都市神豪")
    contract = _contract(1)
    contract.update({
        "payoff_intensity": "high",
        "payoff_arc": ["pressure", "build", "burst", "feedback", "aftershock"],
        "witness_reaction": "",
    })
    contract.pop("payoff_feedback", None)
    result = validate_payoff_contract(contract, profile=profile, required=True)

    assert result["passed"] is True
    assert result["strength_passed"] is False
    assert any("可见反馈" in issue for issue in result["strength_issues"])


def test_payoff_beat_structure_allows_four_beats_to_cover_five_phases():
    result = validate_payoff_beat_structure([
        {"name": "压制", "payoff_phases": ["pressure", "build"]},
        {"name": "选择", "payoff_phase": "build"},
        {"name": "爆发", "payoff_phase": "burst"},
        {"name": "反馈与余波", "payoff_phases": ["feedback", "aftershock"]},
    ])

    assert result["passed"] is True
    assert result["missing_phases"] == []


def test_payoff_beat_structure_repairs_missing_aftershock_before_generation():
    result = repair_payoff_beat_structure([
        {"name": "压力", "payoff_phase": "pressure"},
        {"name": "选择", "payoff_phase": "build"},
        {"name": "兑现", "payoff_phase": "burst"},
        {"name": "反馈", "payoff_phase": "feedback"},
    ])

    assert result["repaired"] is True
    assert result["repaired_phases"] == ["aftershock"]
    assert result["after"]["passed"] is True
    assert result["beats"][-1]["payoff_phases"] == ["feedback", "aftershock"]
