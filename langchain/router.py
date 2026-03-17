# router.py
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage, AIMessage, BaseMessage
from langchain_core.tools import BaseTool


class ToolRouter:
    """
    A lightweight agent router:
    - Binds tools to your internal LLM (llm.bind_tools(tools))
    - Runs an execution loop:
        LLM -> tool_calls? -> execute tools -> feed ToolMessage -> repeat
    """

    def __init__(self, llm, tools: List[BaseTool], max_steps: int = 20):
        self.tools = tools
        self.tool_map = {t.name: t for t in tools}
        self.max_steps = max_steps

        # bind_tools is the key: it exposes tool schemas + descriptions (docstrings) to the model.
        self.llm = llm.bind_tools(tools)

    def run(self, user_goal: str, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Returns:
          {
            "final": str,
            "steps": [...],
            "messages": [...]  # optional debug
          }
        """
        state = state or {}

        system_prompt = (
            "You are a tool-routing agent.\n"
            "Use available tools when needed.\n"
            "If you call tools, call the correct tool with correct arguments.\n"
            "When you have enough information, respond with the final answer.\n"
        )

        messages: List[BaseMessage] = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"Goal: {user_goal}\n\nState JSON:\n{json.dumps(state, ensure_ascii=False)}"),
        ]

        steps: List[Dict[str, Any]] = []

        for _ in range(self.max_steps):
            ai_msg = self.llm.invoke(messages)
            messages.append(ai_msg)

            tool_calls = getattr(ai_msg, "tool_calls", None)

            # 1) 没有 tool_calls -> 认为模型给出了最终答复
            if not tool_calls:
                final_text = ai_msg.content if isinstance(ai_msg, AIMessage) else str(ai_msg)
                return {"final": final_text, "steps": steps, "messages": messages}

            # 2) 有 tool_calls -> 执行每个 tool call，把结果回填 ToolMessage
            for tc in tool_calls:
                name = tc.get("name")
                args = tc.get("args", {})
                call_id = tc.get("id")

                if name not in self.tool_map:
                    # 工具名不匹配，直接把错误回填给模型
                    err = f"Unknown tool: {name}. Available: {list(self.tool_map.keys())}"
                    steps.append({"tool": name, "args": args, "error": err})
                    messages.append(ToolMessage(content=err, tool_call_id=call_id))
                    continue

                tool = self.tool_map[name]

                try:
                    # tool.invoke 会根据 StructuredTool 的 schema 校验参数
                    result = tool.invoke(args)
                    # 统一成字符串回填（你也可以回填 JSON string）
                    if not isinstance(result, str):
                        result_str = json.dumps(result, ensure_ascii=False)
                    else:
                        result_str = result

                    steps.append({"tool": name, "args": args, "result": result})
                    messages.append(ToolMessage(content=result_str, tool_call_id=call_id))

                except Exception as e:
                    err = f"Tool execution failed: {type(e).__name__}: {e}"
                    steps.append({"tool": name, "args": args, "error": err})
                    messages.append(ToolMessage(content=err, tool_call_id=call_id))

        return {
            "final": "Max steps exceeded without a final answer.",
            "steps": steps,
            "messages": messages,
        }