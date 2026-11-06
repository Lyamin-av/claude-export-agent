"""Адаптер Anthropic Messages API (Claude). Здесь же — prompt caching."""
import anthropic

from .base import END, MAX_TOKENS, TOOL_USE, USAGE_KEYS, LLMClient, LLMResponse, ToolCall

# ---------- Кэширование промпта ----------
# Каждый шаг модель получает заново: инструменты + системный промпт + всю историю.
# Начало запроса от шага к шагу не меняется, поэтому его можно кэшировать: повторное
# чтение из кэша стоит ~10% обычной цены. Отмечаем точки кэша (cache_control):
#  1) системный промпт — кэширует описания инструментов и промпт (они идут первыми);
#  2) последний блок истории — кэширует всю переписку до текущего шага.
# API допускает не больше 4 точек, поэтому старую точку в истории снимаем.
# Кэш срабатывает, только если кэшируемое начало длиннее минимума для модели
# (порядка тысячи-двух тысяч токенов), поэтому на первом шаге его может не быть.
CACHE = {"type": "ephemeral"}

STOP_MAP = {"end_turn": END, "tool_use": TOOL_USE, "max_tokens": MAX_TOKENS}


def _mark_history_for_cache(messages: list) -> None:
    for msg in messages:
        if isinstance(msg["content"], list):
            for block in msg["content"]:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    last = messages[-1]
    if isinstance(last["content"], str):
        last["content"] = [{"type": "text", "text": last["content"]}]
    last["content"][-1]["cache_control"] = CACHE


def _dump(block) -> dict:
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json")
    return dict(vars(block))


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(self, model: str, system: str, tools: list[dict], max_tokens: int = 4096):
        self.client = anthropic.Anthropic()  # ключ берётся из ANTHROPIC_API_KEY
        self.model, self.max_tokens = model, max_tokens
        self.system = [{"type": "text", "text": system, "cache_control": CACHE}]
        self.tools = tools  # схемы инструментов уже в формате Anthropic (input_schema)
        self.messages: list[dict] = []

    def start(self, task: str) -> None:
        self.messages = [{"role": "user", "content": task}]

    def chat(self) -> LLMResponse:
        _mark_history_for_cache(self.messages)
        r = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=self.system,
            tools=self.tools, messages=self.messages,
        )
        self.messages.append({"role": "assistant", "content": r.content})
        return LLMResponse(
            stop=STOP_MAP.get(r.stop_reason, r.stop_reason),
            text="\n".join(b.text for b in r.content if b.type == "text").strip(),
            tool_calls=[ToolCall(b.id, b.name, b.input or {})
                        for b in r.content if b.type == "tool_use"],
            usage={k: getattr(r.usage, k, 0) or 0 for k in USAGE_KEYS},
            raw={"stop_reason": r.stop_reason, "content": [_dump(b) for b in r.content]},
        )

    def add_tool_results(self, results):
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id, "content": output, "is_error": is_error}
            for call, output, is_error in results
        ]})

    def drop_last_response(self) -> None:
        if self.messages and self.messages[-1]["role"] == "assistant":
            self.messages.pop()

    def add_note(self, text: str) -> None:
        # Два сообщения user подряд API не принимает, поэтому дописываем
        # в существующее (после tool_result — так требует API).
        last = self.messages[-1]
        if last["role"] != "user":
            self.messages.append({"role": "user", "content": text})
            return
        if isinstance(last["content"], str):
            last["content"] = [{"type": "text", "text": last["content"]}]
        last["content"].append({"type": "text", "text": text})
