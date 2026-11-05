"""
Превращает conversations.json в читаемые Markdown-файлы: один чат — один файл.
Это обычный детерминированный код, без модели: одинаковый вход всегда даёт
одинаковый выход, поэтому в Git видны только реальные изменения переписки.
"""
import json
import re
import shutil
from pathlib import Path

INDEX_NAME = "README.md"


def _slug(title: str, limit: int = 50) -> str:
    s = re.sub(r"[^\w\s-]", "", title.lower(), flags=re.UNICODE)
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:limit].rstrip("-") or "untitled"


def _ts(value: str | None) -> str:
    return (value or "")[:16].replace("T", " ")


def _main_branch(messages: list[dict]) -> tuple[list[dict], int]:
    """В чате могут быть ветки (сообщение отредактировали и пересоздали ответ).
    Берём ветку, которая заканчивается последним сообщением, — её вы и видели в чате."""
    if not messages:
        return [], 0
    by_id = {m["uuid"]: m for m in messages}
    last = max(messages, key=lambda m: m.get("created_at") or "")
    path, cur = [], last
    while cur:
        path.append(cur)
        cur = by_id.get(cur.get("parent_message_uuid"))
    path.reverse()
    return path, len(messages) - len(path)


def _short(value, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _render_message(m: dict) -> str:
    who = "👤 Вы" if m.get("sender") == "human" else "🤖 Claude"
    parts = [f"### {who} · {_ts(m.get('created_at'))}"]
    blocks = m.get("content") or []
    if not blocks and m.get("text"):
        parts.append(m["text"].strip())
    for b in blocks:
        t = b.get("type")
        if t == "text" and (b.get("text") or "").strip():
            parts.append(b["text"].strip())
        elif t == "tool_use":
            parts.append(f"> 🔧 `{b.get('name')}` {_short(b.get('input') or {})}")
        elif t == "tool_result" and b.get("is_error"):
            parts.append(f"> ❌ ошибка инструмента `{b.get('name')}`")
        elif t == "document":
            parts.append(f"> 📎 документ: {b.get('title') or b.get('file_uuid')}")
        elif t == "image":
            parts.append("> 🖼 изображение")
        # thinking, injected_prompt_block, успешные tool_result — служебное, пропускаем
    for a in m.get("attachments") or []:
        parts.append(f"> 📎 вложение: {a.get('file_name')}")
    for f in m.get("files") or []:
        parts.append(f"> 📎 файл: {f.get('file_name')}")
    return "\n\n".join(parts)


def chat_filename(chat: dict) -> str:
    created = (chat.get("created_at") or "")[:10]
    return f"{created}_{_slug(chat.get('name') or '')}_{chat['uuid'][:8]}.md"


def render_chat(chat: dict) -> str:
    msgs, dropped = _main_branch(chat.get("chat_messages") or [])
    title = chat.get("name") or "Без названия"
    head = [
        f"# {title}", "",
        f"- Создан: {_ts(chat.get('created_at'))}",
        f"- Обновлён: {_ts(chat.get('updated_at'))}",
        f"- Сообщений: {len(msgs)}" + (f" (ещё {dropped} в отредактированных ветках не показаны)" if dropped else ""),
        f"- ID: `{chat['uuid']}`",
    ]
    if (chat.get("summary") or "").strip():
        head += ["", f"> {chat['summary'].strip()}"]
    body = "\n\n---\n\n".join(_render_message(m) for m in msgs)
    return "\n".join(head) + "\n\n---\n\n" + body + "\n"


def render_all(conversations_json: Path, out_dir: Path) -> dict:
    chats = json.loads(conversations_json.read_text(encoding="utf-8"))
    if out_dir.exists():
        shutil.rmtree(out_dir)  # пересобираем целиком: удалённые чаты исчезнут
    out_dir.mkdir(parents=True)
    rows = []
    for chat in chats:
        name = chat_filename(chat)
        (out_dir / name).write_text(render_chat(chat), encoding="utf-8")
        shown, _ = _main_branch(chat.get("chat_messages") or [])
        rows.append((chat.get("updated_at") or "", chat.get("name") or "Без названия",
                     len(shown), name))
    rows.sort(reverse=True)
    index = ["# Чаты", "", f"Всего: {len(rows)}", "",
             "| Обновлён | Чат | Сообщений |", "|---|---|---|"]
    index += [f"| {_ts(u)} | [{t.replace('|', '/')}]({n}) | {c} |" for u, t, c, n in rows]
    (out_dir / INDEX_NAME).write_text("\n".join(index) + "\n", encoding="utf-8")
    return {"chats": len(rows), "out_dir": str(out_dir)}


def title_of(md_file: Path) -> str:
    try:
        first = md_file.read_text(encoding="utf-8").split("\n", 1)[0]
        return first.lstrip("# ").strip()
    except OSError:
        return md_file.stem
