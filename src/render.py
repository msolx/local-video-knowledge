from __future__ import annotations

from typing import Any


def timestamp(seconds: float) -> str:
    seconds = max(0, round(float(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def evidence_text(evidence: list[dict[str, Any]], max_gap_seconds: float = 6.0) -> str:
    """Group nearby excerpts for easier video review without losing segment IDs."""
    if not evidence:
        return ""
    groups: list[dict[str, Any]] = []
    for item in sorted(evidence, key=lambda value: (float(value["start"]), float(value["end"]))):
        if groups and float(item["start"]) - float(groups[-1]["end"]) <= max_gap_seconds:
            groups[-1]["end"] = max(float(groups[-1]["end"]), float(item["end"]))
            groups[-1]["segment_ids"].append(item["segment_id"])
        else:
            groups.append({"start": float(item["start"]), "end": float(item["end"]), "segment_ids": [item["segment_id"]]})
    return "; ".join(f"{timestamp(group['start'])}–{timestamp(group['end'])} ({', '.join(group['segment_ids'])})" for group in groups)


def visual_evidence_text(evidence: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in evidence:
        label = f"{timestamp(item['start'])}–{timestamp(item['end'])} [{item['id']}]"
        text = item.get("text") or item.get("content")
        if isinstance(text, dict):
            text = "; ".join(f"{key}: {value}" for key, value in text.items())
        if text:
            lines.append(f"- {label}: {text}")
    return lines


TYPE_HEADINGS = {"author_claim": "作者的事实性主张", "author_opinion": "作者观点与经验", "verification_question": "待核实问题"}
PLATFORM_LABELS = {"douyin": "Douyin", "bilibili": "Bilibili", "youtube": "YouTube", "other": "Other"}


def source_text(source: dict[str, Any]) -> str:
    return f"{PLATFORM_LABELS.get(source.get('platform'), 'Other')} · {source.get('author_name') or '未提供'}"


def _append_visual(lines: list[str], item: dict[str, Any]) -> None:
    visual = item.get("visual_evidence", [])
    if visual:
        lines += ["画面证据：", *visual_evidence_text(visual), ""]


def render_markdown(metadata: dict[str, Any], knowledge: dict[str, Any]) -> str:
    tags, conclusion, source = knowledge.get("keywords", []), knowledge["one_sentence_conclusion"], knowledge.get("source", {})
    lines = ["---", f"video_id: {metadata['video_id']}", f"title: {metadata.get('title') or 'untitled'}", f"duration_seconds: {metadata['duration']}", "tags:"]
    lines += [f"  - {tag}" for tag in tags] or ["  - untagged"]
    lines += ["---", "", "# 来源", "", f"平台：{PLATFORM_LABELS.get(source.get('platform'), 'Other')}", f"作者：{source.get('author_name') or '未提供'}"]
    if source.get("source_url"):
        lines.append(f"原视频：{source['source_url']}")
    lines += [f"采集时间：{source.get('collected_at') or '未提供'}", "", "# 一句话结论", "", "_模型综合摘要（llm_synthesis，非视频中的直接主张）_", "", conclusion["content"], "", f"音频证据：{evidence_text(conclusion['evidence'])}", ""]
    _append_visual(lines, conclusion)
    grouped = {kind: [] for kind in TYPE_HEADINGS}
    for point in knowledge["knowledge_points"]:
        grouped[point["type"]].append(point)
    for kind, heading in TYPE_HEADINGS.items():
        if grouped[kind]:
            lines += [f"# {heading}", ""]
        for point in grouped[kind]:
            lines += [f"## {point['id']} · {point['title']}", "", point["content"], "", f"来源：{source_text(source)}", f"音频证据：{evidence_text(point['evidence'])}", ""]
            _append_visual(lines, point)
    unresolved = knowledge.get("unresolved_visual_references", [])
    if unresolved:
        lines += ["# 待补充视觉信息", ""]
        for item in unresolved:
            lines += [f"- {timestamp(item['start'])}–{timestamp(item['end'])} [{item.get('visual_request_id', item.get('id'))}]：{item.get('reason', '视觉内容尚未解析')}"]
        lines.append("")
    lines += ["# 关键词", ""] + [f"- {tag}" for tag in tags] + [""]
    return "\n".join(lines)


def render_transcript(segments: list[dict[str, Any]]) -> str:
    return "\n".join(f"[{timestamp(item['start'])} - {timestamp(item['end'])}] {item['id']} {item['text']}" for item in segments) + "\n"
