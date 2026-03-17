# lc_llm_adapter.py
from __future__ import annotations

from typing import Any, List, Optional, Dict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.callbacks import CallbackManagerForLLMRun


class InternalChatModel(BaseChatModel):
    """
    LangChain-compatible wrapper for your internal LLM.

    You MUST implement:
    - _call_internal_llm(messages) -> str  (return raw assistant text)
    Optionally, if your internal LLM can natively output tool calls, you can return
    LangChain's tool-call JSON format directly (see Router file notes).
    """

    model_name: str = "internal-llm"

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        # <<<你需要改这里>>>：初始化你自建 LLM client / endpoint / auth
        self._client = None  # e.g. YourInternalClient(...)

    @property
    def _llm_type(self) -> str:
        return "internal_chat_model"

    def _call_internal_llm(self, messages: List[BaseMessage]) -> str:
        """
        <<<你需要改这里>>>
        把 messages 转成你内部 LLM 的输入格式，调用你自己的 LLM，返回 assistant 的 raw text.
        """
        # 示例：把消息转成简单 dict（你按需修改）
        payload = []
        for m in messages:
            role = "user" if m.type == "human" else "assistant" if m.type == "ai" else "system"
            payload.append({"role": role, "content": m.content})

        # <<<你需要改这里>>>：实际调用
        # resp_text = self._client.chat(payload)
        raise NotImplementedError("Implement _call_internal_llm with your internal LLM logic.")

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        text = self._call_internal_llm(messages)

        # 这里先假设你内部 LLM 返回普通文本（不含 tool_calls）
        # Router 会通过 prompt 约束，让模型输出符合 tool calling 的结构（见 router.py）
        ai = AIMessage(content=text)

        return ChatResult(generations=[ChatGeneration(message=ai)])