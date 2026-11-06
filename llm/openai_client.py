"""
Адаптер OpenAI Chat Completions API. Работает с любым совместимым сервером:
Ollama (http://localhost:11434/v1), LM Studio, vLLM, OpenRouter, OpenAI и т. д.

Чем формат отличается от Anthropic:
- системный промпт — первое сообщение с role="system", а не отдельный параметр;
- схема инструмента обёрнута: {"type": "function", "function": {..., "parameters": ...}};
- аргументы вызова приходят JSON-строкой: её надо разобрать, и она может быть битой;
- результат инструмента — отдельное сообщение role="tool" со ссылкой tool_call_id;
  поля is_error нет, поэтому ошибку помечаем в тексте;
- prompt caching в стиле cache_control нет (Ollama сама переиспользует KV-кэш
  для одинакового начала запроса).
"""
import json
import os

from openai import OpenAI

from .base import END, MAX_TOKENS, TOOL_USE, LLMClient, LLMResponse, ToolCall

STOP_MAP = {"stop": END, "tool_calls": TOOL_USE, "length": MAX_TOKENS}
ERROR_PREFIX = "ОШИБКА: "


def to_openai_tools(tools: list[dict]) -> list[dict]:
    """Схемы инструментов из формата Anthropic (input_schema) в формат OpenAI."""
    return [{"type": "function", "function": {
        "name": t["name"], "description": t["description"], "parameters": t["input_schema"],
    }} for t in tools]


def _parse_args(raw: str | None) -> tuple[dict, str | None]:
    if not raw or not raw.strip():
        return {}, None  # инструмент без параметров
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as e:
        return {}, f"аргументы не разбираются как JSON ({e.msg}, позиция {e.pos}): {raw[:300]}"
    if not isinstance(args, dict):
        return {}, f"аргументы должны быть JSON-объектом, получено {type(args).__name__}: {raw[:300]}"
    return args, None


class OpenAIClient(LLMClient):
    provider = "openai"

    def __init__(self, model: str, system: str, tools: list[dict], max_tokens: int = 4096):
        self.client = OpenAI(
            base_url=os.getenv("OPENAI_BASE_URL", "http://localhost:11434/v1"),
            api_key=os.getenv("OPENAI_API_KEY", "ollama"),  # Ollama ключ не проверяет
            timeout=float(os.getenv("LLM_TIMEOUT_SEC", "600")),  # локальная модель бывает медленной
        )
        self.model, self.max_tokens = model, max_tokens
        self.system = system
        self.tools = to_openai_tools(tools)
        temp = os.getenv("TEMPERATURE")
        self.extra = {"temperature": float(temp)} if temp else {}
        self.messages: list[dict] = []

    def start(self, task: str) -> None:
        self.messages = [{"role": "system", "content": self.system},
                         {"role": "user", "content": task}]

    def chat(self) -> LLMResponse:
        r = self.client.chat.completions.create(
            model=self.model, max_tokens=self.max_tokens,
            tools=self.tools, messages=self.messages, **self.extra,
        )
        choice = r.choices[0]
        msg = choice.message

        calls = []
        for i, tc in enumerate(msg.tool_calls or []):
            args, err = _parse_args(tc.function.arguments)
            calls.append(ToolCall(id=tc.id or f"call_{i}", name=tc.function.name,
                                  args=args, parse_error=err))

        # В историю кладём ответ в формате, который API примет обратно.
        assistant = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant["tool_calls"] = [{
                "id": c.id, "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
            } for c, tc in zip(calls, msg.tool_calls)]
        self.messages.append(assistant)

        # Некоторые серверы при вызове инструментов отвечают finish_reason="stop":
        # решаем по факту наличия вызовов, а не только по finish_reason.
        stop = TOOL_USE if calls else STOP_MAP.get(choice.finish_reason, choice.finish_reason)
        u = r.usage
        return LLMResponse(
            stop=stop,
            text=(msg.content or "").strip(),
            tool_calls=calls,
            usage={"input_tokens": getattr(u, "prompt_tokens", 0) or 0,
                   "output_tokens": getattr(u, "completion_tokens", 0) or 0},
            raw={"finish_reason": choice.finish_reason,
                 "message": msg.model_dump(mode="json", exclude_none=True)},
        )

    def add_tool_results(self, results):
        for call, output, is_error in results:
            self.messages.append({"role": "tool", "tool_call_id": call.id,
                                  "content": (ERROR_PREFIX + output) if is_error else output})

    def drop_last_response(self) -> None:
        if self.messages and self.messages[-1]["role"] == "assistant":
            self.messages.pop()

    def add_note(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})
