"""
Превращает conversations.json в читаемые Markdown-файлы: один чат — один файл.
Два вида: render_all — все чаты одним списком (chats/), render_by_project —
те же чаты плюс документы и память проектов по папкам проектов (by_project/).
Это обычный детерминированный код, без модели: одинаковый вход всегда даёт
одинаковый выход, поэтому в Git видны только реальные изменения переписки.
"""
import json
import re
import shutil
from pathlib import Path

import projects as proj

INDEX_NAME = "README.md"
CHATS_SUBDIR = "chats"
NO_PROJECT_DIR = "_без-проекта"
PROJECT_LINE = "- Проект: "
TEXT_SUFFIXES = {".md", ".txt", ".json", ".puml", ".drawio", ".yaml", ".yml", ".csv", ".xml",
                 ".html", ".py", ".sql", ".sh", ".toml", ".ini"}


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


def render_chat(chat: dict, project: str | None = None, source: str = "") -> str:
    msgs, dropped = _main_branch(chat.get("chat_messages") or [])
    title = chat.get("name") or "Без названия"
    head = [
        f"# {title}", "",
        f"- Создан: {_ts(chat.get('created_at'))}",
        f"- Обновлён: {_ts(chat.get('updated_at'))}",
        f"- Сообщений: {len(msgs)}" + (f" (ещё {dropped} в отредактированных ветках не показаны)" if dropped else ""),
        f"- ID: `{chat['uuid']}`",
    ]
    if project is not None:  # только в раскладке по проектам
        head.append(PROJECT_LINE + project)
        if source:
            head.append(f"- Привязка к проекту: {source}")
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


def _project_dirs(projects: dict[str, dict]) -> dict[str, str]:
    """uuid проекта → имя папки. Одинаковые и пустые названия разводим хвостом uuid."""
    slugs = {u: _slug(p["raw"].get("name") or "") for u, p in projects.items()}
    seen: dict[str, int] = {}
    for s in slugs.values():
        seen[s] = seen.get(s, 0) + 1
    return {u: (s if seen[s] == 1 and s != "untitled" else f"{s}_{u[:8]}") for u, s in slugs.items()}


def _safe_relpath(name: str) -> Path:
    """Имя документа из экспорта → безопасный относительный путь (без .., дисков, запрещённых
    в Windows символов). Нетекстовым по расширению файлам добавляем .md: в экспорте лежит
    извлечённый текст, а не исходный docx/pdf."""
    parts = [re.sub(r'[<>:"|?*\x00-\x1f]', "_", p).strip(" .") for p in re.split(r"[\\/]+", name)]
    parts = [p for p in parts if p and p != ".."] or ["untitled"]
    path = Path(*parts)
    return path if path.suffix.lower() in TEXT_SUFFIXES else path.with_name(path.name + ".md")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _chat_table(rows, last_title: str) -> list[str]:
    out = [f"| Обновлён | Чат | Сообщений | {last_title} |", "|---|---|---|---|"]
    out += [f"| {_ts(u)} | [{t.replace('|', '/')}](<{link}>) | {c} | {last} |"
            for u, t, c, link, last in sorted(rows, reverse=True)]
    return out


def render_by_project(conversations_json: Path, out_dir: Path, projects_dir: Path,
                      memories_dir: Path | None = None, overrides_file: Path | None = None) -> dict:
    """Раскладка по проектам: out_dir/<проект>/{README.md, chats/, docs/, memory/}.
    Чаты без проекта — в out_dir/_без-проекта/chats/."""
    chats = json.loads(conversations_json.read_text(encoding="utf-8"))
    projects = proj.load_projects(projects_dir)
    memory = proj.load_memory(memories_dir) if memories_dir else {}
    overrides, errors = (proj.load_overrides(overrides_file, projects)
                         if overrides_file else ({}, []))
    dirs = _project_dirs(projects)
    if out_dir.exists():
        shutil.rmtree(out_dir)  # пересобираем целиком
    out_dir.mkdir(parents=True)

    groups: dict[str | None, list] = {}
    unknown_project = manual = 0
    for chat in chats:
        found = proj.detect(chat, projects, overrides)
        uuid = found["uuid"]
        folder = dirs[uuid] if uuid else NO_PROJECT_DIR
        project = projects[uuid]["name"] if uuid else proj.NO_PROJECT
        name = chat_filename(chat)
        _write(out_dir / folder / CHATS_SUBDIR / name, render_chat(chat, project, found["source"]))
        shown, _ = _main_branch(chat.get("chat_messages") or [])
        unknown_project += bool(found["in_project"] and not uuid)
        manual += found["source"] == "указано вручную"
        last = found["source"] if uuid else (
            f"`{chat['uuid'][:8]}`" + (" ⚠ чат из проекта" if found["in_project"] else ""))
        groups.setdefault(uuid, []).append(
            (chat.get("updated_at") or "", chat.get("name") or "Без названия", len(shown),
             f"{CHATS_SUBDIR}/{name}", last))

    # Страница и материалы каждого проекта (пустые черновики проектов пропускаем)
    listed = []
    for uuid, p in projects.items():
        raw, rows, files = p["raw"], groups.get(uuid, []), memory.get(uuid, [])
        docs = raw.get("docs") or []
        if not (rows or docs or files or (raw.get("name") or "").strip()):
            continue
        base = out_dir / dirs[uuid]
        page = [f"# {p['name']}", "",
                f"- Создан: {_ts(raw.get('created_at'))}",
                f"- Обновлён: {_ts(raw.get('updated_at'))}",
                f"- Чатов: {len(rows)}, документов: {len(docs)}, файлов памяти: {len(files)}",
                f"- ID: `{uuid}`"]
        if (raw.get("description") or "").strip():
            page += ["", "## Описание", "", raw["description"].strip()]
        if (raw.get("prompt_template") or "").strip():
            page += ["", "## Инструкции проекта", "", raw["prompt_template"].strip()]
        if docs:
            page += ["", "## Документы", ""]
            for d in sorted(docs, key=lambda d: d.get("filename") or ""):
                rel = Path("docs") / _safe_relpath(d.get("filename") or d.get("uuid") or "")
                _write(base / rel, d.get("content") or "")
                page.append(f"- [{d.get('filename')}](<{rel.as_posix()}>) · {_ts(d.get('created_at'))}")
        if files:
            page += ["", "## Память проекта", ""]
            for m in sorted(files, key=lambda m: m["path"]):
                rel = Path("memory") / _safe_relpath(m["path"])
                _write(base / rel, m["content"])
                page.append(f"- [{m['path']}](<{rel.as_posix()}>)")
        if rows:
            page += ["", "## Чаты", ""] + _chat_table(rows, "Привязка")
        _write(base / INDEX_NAME, "\n".join(page) + "\n")
        listed.append((max(rows)[0] if rows else (raw.get("updated_at") or ""), uuid, len(rows),
                       len(docs), len(files)))

    loose = groups.get(None, [])
    if loose:
        _write(out_dir / NO_PROJECT_DIR / INDEX_NAME, "\n".join(
            [f"# {proj.NO_PROJECT}", "",
             "В экспорте Claude у чата нет поля «проект», привязка восстановлена по следам",
             "внутри чатов. У этих чатов следов нет: либо они и правда вне проектов, либо",
             f"проект надо указать вручную в `{proj.OVERRIDES_NAME}` в корне архива:",
             "`{\"<ID чата>\": \"<название проекта>\"}`. ⚠ — чат точно вёлся в каком-то проекте.",
             ""] + _chat_table(loose, "ID")) + "\n")

    total = sum(len(v) for v in groups.values())
    index = ["# Проекты", "", f"Чатов: {total}, из них без проекта: {len(loose)}", "",
             "| Проект | Чатов | Документов | Память | Последняя активность |", "|---|---|---|---|---|"]
    for when, uuid, n_chats, n_docs, n_mem in sorted(listed, reverse=True):
        index.append(f"| [{projects[uuid]['name'].replace('|', '/')}](<{dirs[uuid]}/{INDEX_NAME}>) "
                     f"| {n_chats} | {n_docs} | {n_mem} | {_ts(when)} |")
    if loose:
        index.append(f"| [{proj.NO_PROJECT}](<{NO_PROJECT_DIR}/{INDEX_NAME}>) | {len(loose)} | | "
                     f"| {_ts(max(loose)[0])} |")
    _write(out_dir / INDEX_NAME, "\n".join(index) + "\n")
    return {
        "out_dir": str(out_dir), "projects": len(listed),
        "chats_by_project": {projects[u]["name"]: n for _, u, n, _, _ in sorted(listed, reverse=True) if n},
        "chats_without_project": len(loose),
        "of_them_in_unknown_project": unknown_project,
        "manual_overrides_applied": manual,
        "override_errors": errors,
    }


def meta_of(text: str) -> dict:
    """Название, проект и тело чата из готового Markdown (для сводки изменений)."""
    head, _, body = text.partition("\n---\n")
    project = next((ln[len(PROJECT_LINE):].strip() for ln in head.split("\n")
                    if ln.startswith(PROJECT_LINE)), None)
    return {"title": text.split("\n", 1)[0].lstrip("# ").strip(), "project": project, "body": body}


def title_of(md_file: Path) -> str:
    try:
        first = md_file.read_text(encoding="utf-8").split("\n", 1)[0]
        return first.lstrip("# ").strip()
    except OSError:
        return md_file.stem
