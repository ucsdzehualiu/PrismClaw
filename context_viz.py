"""PrismClaw 上下文可视化引擎（ContextViz）。

每轮对话的核心创新：把 Agent 实际"看到"的上下文结构化展示。
把每轮上下文结构化为面向 Web 的 JSON 事件流与可读 Markdown 日志。

输出结构（每轮）：
{
  "round": N,
  "system_blocks": [...],    # System Prompt 分块
  "messages_preview": [...], # 发给 LLM 的消息预览
  "token_stats": {...},      # Token 统计
  "tool_calls": [...],       # 工具调用记录
  "context_changes": [...],  # 上下文变更
  "skill_loads": [...],      # 技能加载记录
}
"""

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class SystemBlock:
    """System Prompt 的一个分块。"""
    name: str = ""
    chars: int = 0
    preview: str = ""


@dataclass
class MessagePreview:
    """一条发给 LLM 的消息预览。"""
    role: str = ""
    chars: int = 0
    preview: str = ""


@dataclass
class TokenStats:
    """Token 统计。"""
    system_tokens: int = 0
    history_tokens: int = 0
    input_tokens: int = 0
    total_tokens: int = 0
    budget: int = 32000
    pct: float = 0.0
    compressed: bool = False
    compress_info: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallRecord:
    """工具调用记录。"""
    step: int = 0
    tool_name: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    result: Any = None
    timestamp: str = ""


@dataclass
class SkillLoadRecord:
    """技能加载记录。"""
    level: str = ""   # L1/L2/L3
    name: str = ""
    chars: int = 0


@dataclass
class RoundContext:
    """单轮完整上下文快照。"""
    round_num: int = 0
    timestamp: str = ""
    user_input: str = ""
    system_blocks: List[SystemBlock] = field(default_factory=list)
    messages: List[MessagePreview] = field(default_factory=list)
    token_stats: TokenStats = field(default_factory=TokenStats)
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    skill_loads: List[SkillLoadRecord] = field(default_factory=list)
    context_changes: List[str] = field(default_factory=list)
    llm_output: str = ""
    # 完整 Prompt：模型本轮实际看到的全部 messages（含 system 全文 + 历史）
    full_system: str = ""
    full_messages: List[Dict[str, Any]] = field(default_factory=list)


class ContextViz:
    """上下文可视化引擎 — 面向 SSE 事件流和 Markdown 落盘。"""

    def __init__(self, log_dir: str, session_id: str):
        self.log_dir = log_dir
        self.session_id = session_id
        self.session_dir = os.path.join(log_dir, session_id)
        os.makedirs(self.session_dir, exist_ok=True)
        os.makedirs(os.path.join(self.session_dir, "snapshots"), exist_ok=True)

        self.round_num = 0
        self._current: RoundContext = RoundContext()

    def start_round(self, user_input: str = ""):
        """开始新的一轮。"""
        self.round_num += 1
        self._current = RoundContext(
            round_num=self.round_num,
            timestamp=datetime.now().isoformat(),
            user_input=user_input,
        )

    def set_system_context(self, system_prompt: str, messages: List[Any]):
        """记录 System Prompt 分块、完整全文，以及发给 LLM 的完整消息列表。"""
        self._current.system_blocks = self._split_system(system_prompt)
        self._current.messages = self._preview_messages(messages)
        # 完整 Prompt：system 全文 + 历史消息完整内容（模型实际看到的全部）
        full = [{"role": "system", "content": system_prompt, "chars": len(system_prompt)}]
        full.extend(self._full_messages(messages))
        self._current.full_system = system_prompt
        self._current.full_messages = full

    def set_tokens(self, stats: TokenStats):
        """记录 Token 统计。"""
        self._current.token_stats = stats

    def add_tool_call(self, step: int, name: str, params: dict, result: Any = None) -> ToolCallRecord:
        """记录工具调用，返回记录对象以便后续回填。"""
        rec = ToolCallRecord(
            step=step,
            tool_name=name,
            params=params,
            result=result,
            timestamp=datetime.now().isoformat(),
        )
        self._current.tool_calls.append(rec)
        return rec

    def update_tool_result(self, step: int, result: Any):
        """更新工具调用结果。"""
        for tc in self._current.tool_calls:
            if tc.step == step:
                tc.result = result
                break

    def update_tool_input(self, step: int, params: dict):
        """回填工具调用参数（流式后块携带更完整的 input，直接覆盖）。"""
        for tc in self._current.tool_calls:
            if tc.step == step:
                tc.params = params
                break

    def add_skill_load(self, level: str, name: str, chars: int = 0):
        """记录技能加载。"""
        self._current.skill_loads.append(SkillLoadRecord(level=level, name=name, chars=chars))

    def add_change(self, text: str):
        """记录上下文变更。"""
        self._current.context_changes.append(text)

    def set_llm_output(self, text: str):
        """记录 LLM 输出。"""
        self._current.llm_output = text

    # ---- 内部方法 ----

    def _split_system(self, system_prompt: str) -> List[SystemBlock]:
        """按 Markdown 标题 (## / ###) 拆分 System Prompt 为结构块。"""
        if not system_prompt:
            return []
        parts = re.split(r"(?m)^(#{2,3}\s+.+)$", system_prompt)
        blocks: List[SystemBlock] = []
        cur_name = "前言 (Preamble)"
        cur_buf: List[str] = []
        for seg in parts:
            if not seg:
                continue
            if re.match(r"(?m)^#{2,3}\s+", seg):
                if cur_buf:
                    text = "\n".join(cur_buf).strip()
                    if text:
                        blocks.append(SystemBlock(
                            name=cur_name,
                            chars=len(text),
                            preview=text[:200] + ("..." if len(text) > 200 else ""),
                        ))
                    cur_buf = []
                cur_name = seg.lstrip("# ").strip()
            else:
                cur_buf.append(seg)
        if cur_buf:
            text = "\n".join(cur_buf).strip()
            if text:
                blocks.append(SystemBlock(
                    name=cur_name,
                    chars=len(text),
                    preview=text[:200] + ("..." if len(text) > 200 else ""),
                ))
        return blocks

    def _full_messages(self, messages: List[Any]) -> List[Dict[str, Any]]:
        """把发给 LLM 的历史消息转换为完整可读的 dict 列表。

        第一块 system 已由 set_system_context 单独放入，这里跳过 role=='system'。
        """
        out: List[Dict[str, Any]] = []
        for m in messages:
            role = getattr(m, "role", "unknown")
            if role == "system":
                continue  # system 已作为 full[0] 完整呈现
            content = getattr(m, "content", "")
            text = self._content_to_text(content)
            out.append({"role": role, "content": text, "chars": len(text)})
        return out

    def _content_to_text(self, content: Any) -> str:
        """把消息 content（str / list[block]）转换为完整可读文本。"""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [self._block_to_text(c) for c in content]
            return "\n\n".join(p for p in parts if p)
        if content is None:
            return ""
        return str(content)

    def _block_to_text(self, c: Any) -> str:
        """把单个 content block（dict 或 AgentScope Block 对象）转为文本。"""
        # dict 形式（流式末尾 / 序列化）
        if isinstance(c, dict):
            t = c.get("type", "")
            if t == "text":
                return str(c.get("text", ""))
            if t == "tool_use":
                name = c.get("name", "")
                inp = c.get("input") or c.get("raw_input")
                if isinstance(inp, str):
                    try:
                        inp = json.loads(inp)
                    except Exception:
                        pass
                return f"🔧 [工具调用] {name}\n参数: {json.dumps(inp, ensure_ascii=False)}"
            if t == "tool_result":
                out = c.get("output", c.get("content", ""))
                if isinstance(out, list):
                    out = "\n".join(self._block_to_text(x) for x in out if isinstance(x, dict))
                return f"📤 [工具结果]\n{out}"
            return str(c.get("text", c))
        # AgentScope block 对象
        t = getattr(c, "type", "")
        if t == "text":
            return str(getattr(c, "text", c))
        if t == "tool_use":
            name = getattr(c, "name", "")
            inp = getattr(c, "input", None) or getattr(c, "raw_input", None)
            if isinstance(inp, str):
                try:
                    inp = json.loads(inp)
                except Exception:
                    pass
            return f"🔧 [工具调用] {name}\n参数: {json.dumps(inp, ensure_ascii=False)}"
        if t == "tool_result":
            out = getattr(c, "output", None) or getattr(c, "content", "")
            if isinstance(out, list):
                out = "\n".join(self._block_to_text(x) for x in out)
            return f"📤 [工具结果]\n{out}"
        text = getattr(c, "text", None)
        if text is None:
            text = getattr(c, "content", None)
        if text is not None:
            return str(text)
        return str(c)

    def _preview_messages(self, messages: List[Any]) -> List[MessagePreview]:
        """生成消息预览。System 只给字数，其他给截断预览（含工具调用/结果）。"""
        out: List[MessagePreview] = []
        for m in messages:
            role = getattr(m, "role", "unknown")
            content = getattr(m, "content", "")
            # 用 _content_to_text 正确解析 tool_use / tool_result 块
            text = self._content_to_text(content)
            if role == "system":
                out.append(MessagePreview(role=role, chars=len(text), preview="(见上方分块)"))
            else:
                out.append(MessagePreview(
                    role=role,
                    chars=len(text),
                    preview=text[:160] + ("..." if len(text) > 160 else ""),
                ))
        return out

    # ---- 序列化 ----

    def to_event(self) -> Dict[str, Any]:
        """转为 SSE 事件 JSON（发给前端）。"""
        c = self._current
        return {
            "type": "context_view",
            "round": c.round_num,
            "timestamp": c.timestamp,
            "user_input": c.user_input,
            "system_blocks": [
                {"name": b.name, "chars": b.chars, "preview": b.preview}
                for b in c.system_blocks
            ],
            "messages": [
                {"role": m.role, "chars": m.chars, "preview": m.preview}
                for m in c.messages
            ],
            "token_stats": {
                "system": c.token_stats.system_tokens,
                "history": c.token_stats.history_tokens,
                "input": c.token_stats.input_tokens,
                "total": c.token_stats.total_tokens,
                "budget": c.token_stats.budget,
                "pct": c.token_stats.pct,
                "compressed": c.token_stats.compressed,
            },
            "tool_calls": [
                {"step": t.step, "tool": t.tool_name, "params": t.params, "result": t.result}
                for t in c.tool_calls
            ],
            "skill_loads": [
                {"level": s.level, "name": s.name, "chars": s.chars}
                for s in c.skill_loads
            ],
            "context_changes": c.context_changes,
            "full_system": c.full_system,
            "full_messages": c.full_messages,
        }

    def commit(self) -> str:
        """落盘：生成 Markdown 日志 + JSON 快照，返回 Markdown 路径。"""
        c = self._current

        # JSON 快照
        jpath = os.path.join(self.session_dir, "snapshots", f"round_{c.round_num:03d}.json")
        with open(jpath, "w", encoding="utf-8") as f:
            json.dump(self.to_event(), f, ensure_ascii=False, indent=2)

        # Markdown 日志
        md = self._render_markdown()
        mdpath = os.path.join(self.session_dir, f"round_{c.round_num:03d}.md")
        with open(mdpath, "w", encoding="utf-8") as f:
            f.write(md)

        return mdpath

    def _render_markdown(self) -> str:
        """渲染 8 模块 Markdown 透明化记录。"""
        c = self._current
        ts = c.token_stats

        lines = [
            f"---",
            f"session_id: {self.session_id}",
            f"timestamp: {c.timestamp}",
            f"round: {c.round_num}",
            f"---",
            f"",
            f"# 第 {c.round_num} 轮 — 上下文透明化记录",
            f"",
            f"## 0. System Prompt 完整全文",
            f"",
            c.full_system or "(空)",
            f"",
            f"## 1. System Prompt 分块",
        ]
        for b in c.system_blocks:
            lines.append(f"### {b.name}")
            lines.append(f"_({b.chars} 字符)_")
            lines.append("")
            lines.append(b.preview)
            lines.append("")

        lines.extend([
            "## 2. Token 统计",
            f"| 类别 | Token 数 |",
            f"|------|----------|",
            f"| 系统提示词 | {ts.system_tokens:,} |",
            f"| 会话历史 | {ts.history_tokens:,} |",
            f"| 当前输入 | {ts.input_tokens:,} |",
            f"| **总计** | **{ts.total_tokens:,}** |",
            f"| 预算上限 | {ts.budget:,} |",
            f"| 使用率 | {ts.pct:.1f}% |",
            "",
        ])
        if ts.compressed:
            lines.append(f"> ⚠️ 本轮发生上下文压缩")
            lines.append("")

        lines.extend([
            "## 3. 消息预览",
        ])
        for i, m in enumerate(c.messages, 1):
            lines.append(f"**{i}.** [{m.role}] ({m.chars} chars): {m.preview}")
        lines.append("")

        lines.extend([
            f"## 4. 当前用户输入",
            f"> {c.user_input}",
            "",
            f"## 5. LLM 输出",
            c.llm_output or "(无文本输出)",
            "",
            "## 6. 工具调用明细",
        ])
        if c.tool_calls:
            lines.append("| 步骤 | 工具 | 参数 |")
            lines.append("|------|------|------|")
            for t in c.tool_calls:
                params = json.dumps(t.params, ensure_ascii=False)[:120]
                lines.append(f"| {t.step} | `{t.tool_name}` | {params} |")
        else:
            lines.append("(本轮无工具调用)")
        lines.append("")

        lines.extend([
            "## 7. 技能加载",
        ])
        if c.skill_loads:
            for s in c.skill_loads:
                lines.append(f"- [{s.level}] {s.name} ({s.chars} 字符)")
        else:
            lines.append("(无新技能加载)")
        lines.append("")

        lines.extend([
            "## 8. 上下文变更",
        ])
        if c.context_changes:
            for ch in c.context_changes:
                lines.append(f"- {ch}")
        else:
            lines.append("(无变更)")

        return "\n".join(lines)
