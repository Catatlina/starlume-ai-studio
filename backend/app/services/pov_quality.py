"""Narrative point-of-view policy shared by generation and review.

The product default is third-person narrative for web novels.  This is a
generation contract first and a deterministic safety check second: quoted
dialogue, messages, letters and direct quotations may contain ``我`` because
that is character voice, while the surrounding narrative may not.
"""
from __future__ import annotations

import re
from typing import Any


THIRD_PERSON_NARRATIVE_POLICY = "third_person_narrative"

# Match longer forms first so ``我们`` is not counted twice as ``我们`` + ``我``.
FIRST_PERSON_PATTERN = re.compile(
    r"我们|咱们|你我|俺们|吾辈|吾等|余等|俺|咱|我|"
    r"(?<![\u4e00-\u9fff])吾(?=[，。！？；：\s]|认为|觉得|想|看|听|说|要|将|便|已|未|不|也|只|可|能|是|乃)|"
    r"(?<![\u4e00-\u9fff])余(?=[，。！？；：\s]|认为|觉得|想|看|听|说|要|将|便|已|未|不|也|只|可|能|是|乃)",
)

# Chinese quotation marks cover dialogue, text messages, letters and direct
# inner speech in the generated prose.  We intentionally keep the detector
# deterministic and conservative: an unmatched quote is not treated as a
# free pass for first-person narration.
QUOTED_SPAN_PATTERN = re.compile(
    r"“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|\"[^\"]*\"|'[^']*'|《[^》]*》",
    flags=re.S,
)

# A small set of fixed reciprocal idioms describes several characters looking
# at one another; it is not narrator POV.  Keep this allowlist narrow rather
# than weakening the general first-person detector.
NON_NARRATIVE_FIRST_PERSON_PATTERN = re.compile(
    r"你看看我\s*[，,、]\s*我看看你|你看我\s*[，,、]\s*我看你"
)


def _mask_quoted_spans(text: str) -> tuple[str, int]:
    """Return text with quoted spans blanked and the number of excluded chars."""
    excluded = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal excluded
        value = match.group(0)
        excluded += len(re.sub(r"\s+", "", value))
        # Preserve line boundaries so excerpts and ratios remain interpretable.
        return re.sub(r"[^\n]", " ", value)

    return QUOTED_SPAN_PATTERN.sub(replace, text or ""), excluded


def analyze_third_person_narrative(text: Any, *, max_examples: int = 8) -> dict[str, Any]:
    """Measure first-person markers outside quoted character voice.

    This is deliberately not a generic Chinese POV classifier.  The product
    requirement is narrower and testable: narrative text must be third-person;
    quoted dialogue/messages/letters may remain first-person.
    """
    source = str(text or "")
    narrative, excluded_chars = _mask_quoted_spans(source)
    narrative = NON_NARRATIVE_FIRST_PERSON_PATTERN.sub(
        lambda match: re.sub(r"[^\n]", " ", match.group(0)),
        narrative,
    )
    matches = list(FIRST_PERSON_PATTERN.finditer(narrative))
    narrative_chars = len(re.sub(r"\s+", "", narrative))
    total_chars = len(re.sub(r"\s+", "", source))
    examples: list[dict[str, Any]] = []
    for match in matches[:max_examples]:
        start = max(0, match.start() - 18)
        end = min(len(source), match.end() + 28)
        excerpt = re.sub(r"\s+", " ", source[start:end]).strip()
        examples.append({"token": match.group(0), "excerpt": excerpt})

    return {
        "policy": THIRD_PERSON_NARRATIVE_POLICY,
        "passed": not matches,
        "first_person_count": len(matches),
        "first_person_tokens": sorted({match.group(0) for match in matches}),
        "examples": examples,
        "narrative_chars": narrative_chars,
        "excluded_quoted_chars": excluded_chars,
        "total_chars": total_chars,
        "first_person_per_1000": round(
            len(matches) / max(1, narrative_chars) * 1000,
            3,
        ),
        "rule": "引号/书名号内的对白、短信、书信和直接引用允许第一人称；固定互视短语不视为第一人称叙事；其余叙述不允许。",
    }


def third_person_generation_contract() -> str:
    """Return the high-priority instruction injected before prose generation."""
    return (
        "【最高优先级：第三人称叙述硬约束】\n"
        "本书正文统一采用第三人称限知叙述。叙述句只能用角色名、‘他/她’或动作承接，"
        "不得用‘我、我们、咱们、俺、吾、余’承担叙述。\n"
        "只有人物对白、短信/聊天、书信、书中引文和明确标注的直接内心独白可以出现第一人称；"
        "不要为了规避规则把正常对白改成第三人称。\n"
        "禁止写‘我看见/我觉得/我想/我掏出/我看着/我感觉到’等叙述句；改成角色名或‘他/她’"
        "承接，例如‘周衡掏出手机’、‘他看见’、‘她感觉到’，但不要连续机械重复角色名。\n"
        "每写完一个段落，先在脑中暂时删去引号内内容做视角自检：剩余叙述不得出现第一人称；"
        "发现误写时在输出前改掉。该约束优先于文风卡、学习规则和重写反馈。"
    )
