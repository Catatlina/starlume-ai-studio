from app.services.pov_quality import analyze_third_person_narrative


def test_reciprocal_look_idiom_is_not_first_person_narration():
    result = analyze_third_person_narrative("五个人你看看我，我看看你。顾沉没有催。")

    assert result["passed"] is True
    assert result["first_person_count"] == 0


def test_real_first_person_narration_still_fails_closed():
    result = analyze_third_person_narrative("我推开门，看见灯火已经熄灭。")

    assert result["passed"] is False
    assert result["first_person_count"] == 1


def test_wu_inside_common_word_is_not_first_person_narration():
    result = analyze_third_person_narrative(
        "赵小胖脸色一僵，支吾了两声：‘就……路过，路过。’"
    )

    assert result["passed"] is True
    assert result["first_person_count"] == 0


def test_standalone_classical_wu_still_fails_closed():
    result = analyze_third_person_narrative("吾将把这笔账记在心里。")

    assert result["passed"] is False
    assert result["first_person_tokens"] == ["吾"]
