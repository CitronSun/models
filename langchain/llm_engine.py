# internal_llm_client.py
from __future__ import annotations

from typing import Any, Dict, List, Optional
import requests

Message = Dict[str, str]  # {"role": "...", "content": "..."}


class InternalLLMClient:
    """
    你的内部 LLM client：输入 messages(list[dict])，输出纯字符串 content。
    """

    def __init__(self, *, endpoint: str, model: str, api_key: Optional[str] = None, timeout: int = 60):
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def chat(self, messages: List[Message], *, temperature: float = 0.0, extra: Optional[Dict[str, Any]] = None) -> str:
        payload: Dict[str, Any] = {
            "model": self.model,
            "temperature": temperature,
            "messages": messages,
        }
        if extra:
            payload.update(extra)  # 例如 top_p、max_tokens、stop 等你内部支持的字段

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            # <<<你需要改这里>>>：按你们内部鉴权方式改 header
            headers["Authorization"] = f"Bearer {self.api_key}"

        resp = requests.post(self.endpoint, json=payload, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()

        # <<<你需要改这里>>>：如果你们返回结构不是这个路径，就改这里
        return data["choices"][0]["message"]["content"]