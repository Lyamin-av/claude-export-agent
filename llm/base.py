"""
Общий интерфейс к LLM. Ядро агента (agent.py) знает только эти типы
и не знает, в каком формате общается конкретный провайдер.

Каждый адаптер сам хранит историю диалога в «родном» формате API:
у Anthropic результаты инструментов — блоки tool_result внутри сообщения user,
у OpenAI — отдельные сообщения с role="tool". Ядро только говорит адаптеру,
что добавить в историю.
"""
from dataclasses import dataclass, field

# Нейтральные причины остановки модели
END = "end"              # модель закончила и ответила текстом
TOOL_USE = "tool_use"    # модель просит вызвать инструменты
MAX_TOKENS = "max_tokens"  # ответ оборвался по лимиту длины


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict
    # Если модель прислала аргументы, которые не разбираются как JSON-объект,
    # здесь текст ошибки. Ядро не исполняет такой вызов, а возвращает ошибку модели.
    parse_error: str | None = None


@dataclass
class LLMResponse:
    stop: str                     # END / TOOL_USE / MAX_TOKENS / иная причина как есть
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)   # ключи как в USAGE_KEYS
    raw: dict = field(default_factory=dict)     # ответ провайдера целиком — для журнала


USAGE_KEYS = ("input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens")


class LLMClient:
    """Интерфейс адаптера. Методы истории вызываются ядром в таком порядке:
    start(task) -> [chat() -> add_tool_results(...) | drop_last_response() + add_note(...)]..."""

    provider: str
    model: str

    def start(self, task: str) -> None:
        """Начать диалог с задачи пользователя."""
        raise NotImplementedError

    def chat(self) -> LLMResponse:
        """Отправить историю модели, добавить её ответ в историю и вернуть его."""
        raise NotImplementedError

    def add_tool_results(self, results: list[tuple[ToolCall, str, bool]]) -> None:
        """Добавить результаты вызовов: (вызов, текст результата, была ли ошибка)."""
        raise NotImplementedError

    def drop_last_response(self) -> None:
        """Убрать из истории последний ответ модели (например, оборванный)."""
        raise NotImplementedError

    def add_note(self, text: str) -> None:
        """Добавить пояснение для модели от имени пользователя."""
        raise NotImplementedError
