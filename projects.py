"""
Определяет, к какому проекту Claude относится чат.

В экспорте claude.ai прямой связи нет: в conversations.json у чата нет поля
проекта, а в projects/*.json нет списка чатов. Поэтому проект восстанавливается
по следам внутри самого чата (по убыванию надёжности):

1. ручная привязка в файле chat_projects.json в корне архива;
2. список памяти, который система показывает модели: в чате проекта файлы
   памяти этого проекта перечислены по одному, в обычном чате проекты свёрнуты;
3. запись в память проекта: из чата проекта писать можно только в его папку;
4. чтение памяти одного-единственного проекта;
5. обращения к документам проекта: имя документа ищется в projects/*.json.

Это детерминированный код без модели. Если следов нет, чат остаётся без проекта —
лучше честное «не знаю», чем догадка.
"""
import json
import re
from collections import Counter
from pathlib import Path

OVERRIDES_NAME = "chat_projects.json"
NO_PROJECT = "Без проекта"

_PROJECT_PATH = re.compile(r"/projects/([0-9a-f]{8}-[0-9a-f-]{27})/([^\s\"'\\\]]*)")
_MEMORY_WRITE = ("memory_write", "memory_append", "memory_str_replace", "memory_delete")


def load_projects(projects_dir: Path) -> dict[str, dict]:
    """uuid проекта → {"name", "docs"}. Понимает и файл на проект, и общий список."""
    found: dict[str, dict] = {}
    if not projects_dir.exists():
        return found
    for f in sorted(projects_dir.rglob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for p in data if isinstance(data, list) else [data]:
            if isinstance(p, dict) and p.get("uuid"):
                found[p["uuid"]] = {
                    "name": (p.get("name") or "").strip() or f"Без названия ({p['uuid'][:8]})",
                    "docs": {d.get("filename") for d in p.get("docs") or [] if d.get("filename")},
                    "raw": p,
                }
    return found


def load_memory(memories_dir: Path) -> dict[str, list[dict]]:
    """uuid проекта → файлы памяти этого проекта [{"path" (внутри проекта), "content"}]."""
    result: dict[str, list[dict]] = {}
    if not memories_dir.exists():
        return result
    for f in sorted(memories_dir.rglob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in (data.get("memory_files") or []) if isinstance(data, dict) else []:
            m = _PROJECT_PATH.match(item.get("path") or "")
            if m and m.group(2):
                result.setdefault(m.group(1), []).append(
                    {"path": m.group(2), "content": item.get("content") or ""})
    return result


def load_overrides(path: Path, projects: dict[str, dict]) -> tuple[dict[str, str | None], list[str]]:
    """Ручные привязки: {"<uuid чата или первые 8 символов>": "<название или uuid проекта>"}.
    Пустое значение или null — «этот чат точно без проекта»."""
    if not path.exists():
        return {}, []
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as e:
        return {}, [f"{path.name}: не разобрать JSON ({e})"]
    if not isinstance(raw, dict):
        return {}, [f"{path.name}: ожидается объект {{чат: проект}}"]
    by_name: dict[str, list[str]] = {}
    for uuid, p in projects.items():
        by_name.setdefault(p["name"].casefold(), []).append(uuid)
    result, errors = {}, []
    for chat, value in raw.items():
        if chat.startswith("_"):  # "_comment" и подобное
            continue
        if value in (None, ""):
            result[chat] = None
            continue
        value = str(value).strip()
        hits = [u for u in projects if u == value or (len(value) >= 8 and u.startswith(value))]
        hits = hits or by_name.get(value.casefold(), [])
        if len(hits) == 1:
            result[chat] = hits[0]
        elif not hits:
            errors.append(f"{path.name}: для чата {chat} указан неизвестный проект «{value}»")
        else:
            errors.append(f"{path.name}: проектов с названием «{value}» несколько, укажите uuid")
    return result, errors


def _blocks(chat: dict):
    for m in chat.get("chat_messages") or []:
        yield from m.get("content") or []


def detect(chat: dict, projects: dict[str, dict],
           overrides: dict[str, str | None] | None = None) -> dict:
    """Возвращает {"uuid": проект или None, "source": откуда известно, "in_project": bool}.
    in_project=True при uuid=None значит: чат вёлся в каком-то проекте, но в каком —
    по экспорту не определить."""
    overrides = overrides or {}
    for key in (chat["uuid"], chat["uuid"][:8]):
        if key in overrides:
            return {"uuid": overrides[key], "source": "указано вручную", "in_project": bool(overrides[key])}

    listing, written, read, docs = Counter(), Counter(), Counter(), Counter()
    used_projects_tool = False
    for b in _blocks(chat):
        kind, name = b.get("type"), b.get("name") or ""
        if kind == "injected_prompt_block" and b.get("injection_source") == "memory_block_head":
            for uuid, rest in _PROJECT_PATH.findall(b.get("prompt") or ""):
                if rest:  # путь к файлу, а не свёрнутая папка проекта
                    listing[uuid] += 1
        elif kind == "tool_use":
            args = b.get("input") or {}
            if name.endswith(_MEMORY_WRITE):
                for uuid, _ in _PROJECT_PATH.findall(json.dumps(args.get("path", ""))):
                    written[uuid] += 1
            elif name.endswith("memory_read"):
                for uuid, _ in _PROJECT_PATH.findall(json.dumps(args.get("path", ""))):
                    read[uuid] += 1
            elif name == "Projects":
                used_projects_tool = True
                path = args.get("path")
                if isinstance(path, str) and path:
                    owners = [u for u, p in projects.items()
                              if path in p["docs"] or f"claude/{path}" in p["docs"]]
                    if len(owners) == 1:  # одноимённые документы в разных проектах не считаем
                        docs[owners[0]] += 1

    for votes, source in ((listing, "список памяти проекта"), (written, "запись в память проекта"),
                          (read, "чтение памяти проекта"), (docs, "документы проекта")):
        known = [u for u in votes if u in projects]
        if len(known) == 1:
            return {"uuid": known[0], "source": source, "in_project": True}
    in_project = used_projects_tool or bool(listing or written)
    return {"uuid": None, "source": "проект не определён" if in_project else "", "in_project": in_project}
