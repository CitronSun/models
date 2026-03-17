# json_router.py
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from langchain_core.tools import BaseTool


@dataclass
class LLMMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


class InternalLLMClient:
    """
    <<<你需要改这里>>>
    这是你内部 LLM 的调用封装：
    - 输入 messages（role/content）
    - 返回一个纯字符串（choices[0].message.content）
    """
    def chat(self, messages: List[LLMMessage], *, temperature: float = 0.0) -> str:
        raise NotImplementedError


class JSONToolRouter:
    """
    Router 工作方式：
    1) 把 tools（name + docstring + args_schema）渲染进 prompt
    2) 调 internal LLM，让它严格输出 JSON：
       - {"action":"call_tool","tool_name":"...","args":{...}}
       - {"action":"final","final":"..."}
    3) 如果 call_tool：执行对应 tool.invoke(args)，把结果再喂回 LLM，继续 loop
    """

    def __init__(
        self,
        llm: InternalLLMClient,
        tools: List[BaseTool],
        *,
        max_steps: int = 15,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.tool_map = {t.name: t for t in tools}
        self.max_steps = max_steps

    def run(self, *, goal: str, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        state = state or {}

        messages: List[LLMMessage] = [
            LLMMessage(
                role="system",
                content=self._build_system_prompt(),
            ),
            LLMMessage(
                role="user",
                content=self._build_user_prompt(goal=goal, state=state),
            ),
        ]

        steps: List[Dict[str, Any]] = []

        for _ in range(self.max_steps):
            raw = self.llm.chat(messages, temperature=0.0)
            messages.append(LLMMessage(role="assistant", content=raw))

            decision = self._parse_json(raw)

            if decision["action"] == "final":
                return {"final": decision["final"], "steps": steps}

            if decision["action"] != "call_tool":
                # 非法 action：直接终止（也可以改成 HITL）
                return {"final": f"Router error: invalid action {decision['action']}", "steps": steps}

            tool_name = decision["tool_name"]
            args = decision.get("args", {})

            if tool_name not in self.tool_map:
                err = f"Unknown tool: {tool_name}"
                steps.append({"tool": tool_name, "args": args, "error": err})
                # 把错误反馈给模型，让它换个工具/修正
                messages.append(LLMMessage(role="user", content=f"TOOL_ERROR: {err}"))
                continue

            tool = self.tool_map[tool_name]

            try:
                result = tool.invoke(args)  # StructuredTool 会做基本参数校验
                steps.append({"tool": tool_name, "args": args, "result": result})

                # 把工具结果喂回模型，让它决定下一步
                result_str = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                messages.append(
                    LLMMessage(
                        role="user",
                        content=f"TOOL_RESULT ({tool_name}): {result_str}",
                    )
                )

            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                steps.append({"tool": tool_name, "args": args, "error": err})
                messages.append(LLMMessage(role="user", content=f"TOOL_ERROR ({tool_name}): {err}"))

        return {"final": "Max steps exceeded", "steps": steps}

    # ---------- Prompt building ----------

    def _build_system_prompt(self) -> str:
        tools_block = self._render_tools()

        return (
            "You are a tool-routing agent.\n"
            "You must decide the next action using the available tools.\n\n"
            "RULES:\n"
            "1) Output ONLY valid JSON. No markdown. No extra text.\n"
            "2) JSON schema must be exactly one of:\n"
            '   {"action":"call_tool","tool_name":"<tool>","args":{...}}\n'
            '   {"action":"final","final":"<answer>"}\n'
            "3) tool_name must be one of the available tools.\n"
            "4) args must match the tool's args_schema.\n\n"
            "AVAILABLE TOOLS:\n"
            f"{tools_block}\n"
        )

    def _build_user_prompt(self, *, goal: str, state: Dict[str, Any]) -> str:
        return (
            f"GOAL:\n{goal}\n\n"
            f"STATE (JSON):\n{json.dumps(state, ensure_ascii=False)}\n"
        )

    def _render_tools(self) -> str:
        lines: List[str] = []
        for t in self.tools:
            # args_schema 是 pydantic model（StructuredTool 会有）
            schema = {}
            if getattr(t, "args_schema", None) is not None:
                schema = t.args_schema.model_json_schema()  # pydantic v2
            desc = (t.description or "").strip()

            lines.append(
                f"- name: {t.name}\n"
                f"  description: {desc}\n"
                f"  args_schema: {json.dumps(schema, ensure_ascii=False)}\n"
            )
        return "\n".join(lines)

    # ---------- JSON parsing ----------

    def _parse_json(self, raw: str) -> Dict[str, Any]:
        """
        强约束：模型必须只输出 JSON。
        如果你担心模型偶尔输出前后夹杂文字，可以在这里做更强的抽取（比如取首个 { ... }）。
        """
        try:
            obj = json.loads(raw)
        except Exception as e:
            # 这里可以做更鲁棒的“抽取 JSON”逻辑；先给你一个直观报错
            raise ValueError(f"LLM output is not valid JSON: {raw[:400]}") from e

        if not isinstance(obj, dict):
            raise ValueError("LLM JSON must be an object")

        action = obj.get("action")
        if action == "final":
            if not isinstance(obj.get("final"), str):
                raise ValueError("final must be a string")
        elif action == "call_tool":
            if not isinstance(obj.get("tool_name"), str):
                raise ValueError("tool_name must be a string")
            if not isinstance(obj.get("args", {}), dict):
                raise ValueError("args must be an object")
        else:
            raise ValueError(f"Unknown action: {action}")

        return obj