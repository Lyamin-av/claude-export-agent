"""
Инструменты агента. Каждый инструмент — это обычная Python-функция
плюс JSON-схема, по которой модель понимает, когда и как её вызвать.

Модель НИЧЕГО не исполняет сама: она только просит "вызови X с аргументами Y",
а исполняет код в agent.py. Поэтому все проверки безопасности живут здесь.

Скачивание с claude.ai требует вашего входа в аккаунт, поэтому файлы
качает ваш браузер: агент только открывает в нём ссылки и следит
за папкой "Загрузки".
"""
import difflib
import email
import html
import imaplib
import json
import os
import re
import shutil
import subprocess
import time
import webbrowser
import zipfile
from datetime import datetime, timezone
from email.policy import default as default_policy
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import projects
import render

# ---------- Настройки (из .env) ----------
WORK_DIR = Path(os.getenv("WORK_DIR", "work")).resolve()
ARCHIVE_DIR = Path(os.getenv("ARCHIVE_DIR", "archive")).resolve()
DOWNLOADS_DIR = Path(os.getenv("DOWNLOADS_DIR", Path.home() / "Downloads")).resolve()
REPO_URL = os.getenv("REPO_URL", "")
MAIL_QUERY = os.getenv("EXPORT_MAIL_QUERY", "(from:anthropic.com OR from:claude.ai)")

# Открывать в браузере можно только ссылки экспорта claude.ai. Ссылки приходят
# из письма, т.е. из недоверенного источника — модель не должна открывать что угодно.
ALLOWED_HOSTS = ("claude.ai",)
GITHUB_FILE_LIMIT = 95 * 1024 * 1024  # GitHub отвергает файлы > 100 МБ
PARTIAL_SUFFIXES = (".crdownload", ".part", ".tmp", ".download")  # недокачанные файлы браузеров


def _export_link(url: str) -> bool:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return (p.scheme == "https" and host in ALLOWED_HOSTS
            and p.path.startswith("/export/"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- Gmail ----------
IMAP_TIMEOUT_SEC = 30  # без таймаута imaplib может ждать ответа сервера бесконечно


def _imap():
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", timeout=IMAP_TIMEOUT_SEC)
        m.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"].replace(" ", ""))
        m.select("INBOX", readonly=True)
    except (TimeoutError, OSError) as e:
        raise ConnectionError(
            f"Gmail (imap.gmail.com:993) не ответил за {IMAP_TIMEOUT_SEC} с при подключении: "
            f"{type(e).__name__}: {e}") from e
    return m


def find_export_emails(days: int = 7) -> dict:
    """Ищет письма от Anthropic. Возвращает только метаданные и ссылки экспорта —
    полный текст письма модели не отдаём."""
    query = f"newer_than:{int(days)}d {MAIL_QUERY}"
    m = _imap()
    try:
        _, data = m.uid("SEARCH", "X-GM-RAW", f'"{query}"')
        uids = data[0].split()[-10:]  # не больше 10 последних
        print(f"   … писем найдено: {len(data[0].split())}, читаю {len(uids)}", flush=True)
        result = []
        for uid in reversed(uids):
            try:
                _, msg_data = m.uid("FETCH", uid, "(BODY.PEEK[])")
            except (TimeoutError, OSError) as e:
                raise ConnectionError(f"Gmail не отдал письмо uid={uid.decode()} за "
                                      f"{IMAP_TIMEOUT_SEC} с: {e}") from e
            msg = email.message_from_bytes(msg_data[0][1], policy=default_policy)
            links = set()
            for part in msg.walk():
                if part.get_content_type() in ("text/plain", "text/html"):
                    text = html.unescape(part.get_content())
                    links.update(u for u in re.findall(r"https://[^\s\"'<>)]+", text)
                                 if _export_link(u))
            sent = parsedate_to_datetime(msg["Date"])
            result.append({
                "uid": uid.decode(),
                "subject": msg["Subject"],
                "sent_at": sent.isoformat(),
                "minutes_ago": round((_now() - sent).total_seconds() / 60),
                "export_links": sorted(links),
            })
        return {"query": query, "emails": result}
    finally:
        m.logout()


# ---------- Браузер и папка "Загрузки" ----------
def _opened_log() -> Path:
    return WORK_DIR / "opened_links.json"


def _was_opened(url: str) -> bool:
    p = _opened_log()
    return p.exists() and url in json.loads(p.read_text(encoding="utf-8"))


def _mark_opened(url: str) -> None:
    p = _opened_log()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    data[url] = _now().isoformat()
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _open(url: str) -> None:
    if not _export_link(url):
        raise ValueError(f"Это не ссылка экспорта claude.ai, открывать не буду: {url}")
    if not webbrowser.open(url):
        raise RuntimeError("Не удалось открыть браузер по умолчанию.")
    _mark_opened(url)


def open_in_browser(url: str) -> dict:
    """Открывает ссылку экспорта в браузере пользователя (там есть вход в claude.ai)."""
    if _was_opened(url):
        return {"status": "already_opened",
                "note": "Эту ссылку уже открывали; ссылки одноразовые, повторно не открываю."}
    _open(url)
    return {"status": "opened", "downloads_dir": str(DOWNLOADS_DIR)}


def _find_in_downloads(filename: str, not_older_than: datetime) -> dict:
    """Ищет файл в Загрузках с учётом переименований браузера: name (1).zip и т.п."""
    stem, suffix = Path(filename).stem, Path(filename).suffix
    pattern = re.compile(rf"^{re.escape(stem)}( ?\(\d+\))?{re.escape(suffix)}$")
    complete, partial = [], []
    if DOWNLOADS_DIR.exists():
        for p in DOWNLOADS_DIR.iterdir():
            mtime = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
            if mtime < not_older_than:
                continue
            name = p.name
            if name.endswith(PARTIAL_SUFFIXES):
                if pattern.match(name.rsplit(".", 1)[0]):
                    partial.append(p)
            elif pattern.match(name):
                complete.append(p)
    complete.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return {"complete": complete, "partial": partial}


def wait_for_manifest(max_age_minutes: int = 120, timeout_sec: int = 120) -> dict:
    """Ждёт появления манифеста экспорта в папке Загрузки."""
    since = datetime.fromtimestamp(time.time() - max_age_minutes * 60, timezone.utc)
    deadline = time.time() + timeout_sec
    while True:
        found = sorted(
            (p for p in DOWNLOADS_DIR.glob("manifest*.json")
             if datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) >= since),
            key=lambda p: p.stat().st_mtime, reverse=True)
        for p in found:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                if "data_files" in d:
                    dest = WORK_DIR / "inbox" / p.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dest)
                    return {"status": "found", "manifest_path": str(dest),
                            "downloaded_minutes_ago": round((time.time() - p.stat().st_mtime) / 60)}
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # ещё дописывается или чужой файл
        if time.time() > deadline:
            return {"status": "not_found", "searched_dir": str(DOWNLOADS_DIR),
                    "waited_sec": timeout_sec,
                    "fact": f"В папке нет файлов manifest*.json за последние {max_age_minutes} мин."}
        time.sleep(3)


def read_manifest(path: str) -> dict:
    """Разбирает манифест экспорта: список файлов и их свежесть."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    created = datetime.fromisoformat(d["created_at"])
    age_h = (_now() - created).total_seconds() / 3600
    local = created.astimezone()  # часовой пояс компьютера пользователя
    return {
        "created_at": d["created_at"],
        "created_at_local": local.strftime("%Y-%m-%d %H:%M"),
        "export_date_local": local.strftime("%Y-%m-%d"),
        "age_hours": round(age_h, 1),
        "links_older_than_24h": age_h > 24,
        "total_files": d.get("total_files"),
        "files": [{"filename": f["filename"], "category": f["category"]} for f in d["data_files"]],
    }


def _export_dir(d: dict) -> Path:
    """Папка для zip одного экспорта. Ключ — точное время создания экспорта,
    а не дата: за один день может быть несколько экспортов."""
    stamp = d["created_at"][:19].replace(":", "-")  # 2026-11-05T20-47-35
    return WORK_DIR / "downloads" / stamp


def fetch_export_files(manifest_path: str, timeout_sec: int = 900) -> dict:
    """Получает все zip из манифеста через браузер: открывает каждую ссылку один раз,
    ждёт файлы в Загрузках, проверяет их и переносит в рабочую папку."""
    d = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    created = datetime.fromisoformat(d["created_at"])
    out_dir = _export_dir(d)
    out_dir.mkdir(parents=True, exist_ok=True)

    pending = {}
    for f in d["data_files"]:
        target = out_dir / f["filename"]
        if target.exists() and zipfile.is_zipfile(target):
            continue
        pending[f["filename"]] = f

    # Открываем ссылки только для тех файлов, которых ещё нет в Загрузках
    opened, skipped = [], []
    for name, f in pending.items():
        if _find_in_downloads(name, created)["complete"]:
            continue
        if _was_opened(f["export_url"]):
            skipped.append(name)
            continue
        _open(f["export_url"])
        opened.append(name)
        time.sleep(1.5)  # чтобы браузер не воспринял это как атаку загрузками

    status = {}
    deadline = time.time() + timeout_sec
    while pending and time.time() < deadline:
        for name in list(pending):
            hits = _find_in_downloads(name, created)
            for p in hits["complete"]:
                size = p.stat().st_size
                time.sleep(0.5)
                if p.stat().st_size == size and zipfile.is_zipfile(p):
                    shutil.move(str(p), out_dir / name)
                    status[name] = {"status": "ok", "size": size}
                    pending.pop(name)
                    break
        if pending:
            time.sleep(3)

    for name in pending:
        hits = _find_in_downloads(name, created)
        status[name] = {
            "status": "missing",
            "link_opened_now": name in opened,
            "link_opened_earlier": name in skipped,
            "partial_files_in_downloads": [p.name for p in hits["partial"]],
            "broken_files_in_downloads": [p.name for p in hits["complete"]],
        }
    already = [f["filename"] for f in d["data_files"] if f["filename"] not in status]
    for name in already:
        status[name] = {"status": "already_in_work_dir"}
    return {
        "download_dir": str(out_dir),
        "manifest_age_hours": round((_now() - created).total_seconds() / 3600, 1),
        "waited_sec": timeout_sec if pending else None,
        "files": status,
    }


# ---------- Распаковка ----------
def _safe_extract(zf: zipfile.ZipFile, target: Path) -> int:
    """Распаковка с защитой от zip-slip (путей вида ../../)."""
    n = 0
    for member in zf.infolist():
        dest = (target / member.filename).resolve()
        if not str(dest).startswith(str(target.resolve())):
            raise ValueError(f"Подозрительный путь в архиве: {member.filename}")
        if not member.is_dir():
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            n += 1
    return n


def unpack_to_archive(manifest_path: str) -> dict:
    """Распаковывает скачанные zip в папку репозитория: ARCHIVE_DIR/<category>/.
    Старое содержимое категории заменяется — в Git останется история версий.
    Сами zip без изменений копируются в ARCHIVE_DIR/archives/<время экспорта>/."""
    d = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    src_dir = _export_dir(d)
    by_cat: dict[str, list] = {}
    for f in d["data_files"]:
        by_cat.setdefault(f["category"], []).append(f)
    summary = []
    for cat, files in by_cat.items():
        zips = [src_dir / Path(f["filename"]).name for f in files]
        missing = [z.name for z in zips if not z.exists()]
        if missing:
            summary.append({"category": cat, "status": "skipped", "missing": missing})
            continue
        target = ARCHIVE_DIR / cat
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        count = 0
        for z in zips:
            with zipfile.ZipFile(z) as zf:
                count += _safe_extract(zf, target)
        summary.append({"category": cat, "status": "ok", "files": count})
    # Оригиналы храним рядом: распакованные данные со временем перезаписываются,
    # а zip — это ровно то, что отдал claude.ai. У каждого экспорта своя папка.
    zips_dir = ARCHIVE_DIR / ZIPS_DIR_NAME / src_dir.name
    saved = []
    for f in d["data_files"]:
        z = src_dir / Path(f["filename"]).name
        if z.exists():
            zips_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(z, zips_dir / z.name)
            saved.append({"file": z.name, "bytes": z.stat().st_size})
    # Манифест кладём без ссылок: они одноразовые и в репозитории не нужны
    clean = {k: v for k, v in d.items() if k != "instructions"}
    clean["data_files"] = [{k: v for k, v in f.items() if k != "export_url"}
                           for f in d["data_files"]]
    (ARCHIVE_DIR / "export_manifest.json").write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"archive_dir": str(ARCHIVE_DIR), "categories": summary,
            "original_zips_dir": f"{ZIPS_DIR_NAME}/{src_dir.name}", "original_zips": saved,
            "original_zips_total_mb": round(sum(s["bytes"] for s in saved) / 1e6, 1)}


# ---------- Читаемый вид и сводка изменений ----------
CHATS_DIR_NAME = "chats"
LIST_LIMIT = 200  # максимум названий в сводке
MOVED_LIMIT = 20  # переносы между проектами: дальше хватит чисел по маршрутам
BY_PROJECT_DIR_NAME = "by_project"  # те же чаты + документы и память, по папкам проектов
ZIPS_DIR_NAME = "archives"  # исходные zip экспорта, как их отдал claude.ai
# Папки, которые целиком строятся из остальных: в «прочих изменениях» их не считаем
DERIVED_DIRS = (CHATS_DIR_NAME, BY_PROJECT_DIR_NAME)


def render_conversations() -> dict:
    """Раскладывает переписку из conversations/*.json по Markdown-файлам двумя способами:
    chats/ — все чаты одним списком; by_project/<проект>/ — чаты, документы и память
    по проектам. Проект чата определяет projects.py; ручные привязки — в
    chat_projects.json в корне архива."""
    files = sorted((ARCHIVE_DIR / "conversations").rglob("*.json"))
    if not files:
        raise FileNotFoundError("В архиве нет conversations/*.json — сначала unpack_to_archive.")
    if len(files) > 1:  # большой экспорт может прийти несколькими частями
        merged = []
        for f in files:
            merged += json.loads(f.read_text(encoding="utf-8"))
        tmp = WORK_DIR / "conversations_merged.json"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        source = tmp
    else:
        source = files[0]
    result = render.render_all(source, ARCHIVE_DIR / CHATS_DIR_NAME)
    result["by_project"] = render.render_by_project(
        source, ARCHIVE_DIR / BY_PROJECT_DIR_NAME,
        projects_dir=ARCHIVE_DIR / "projects", memories_dir=ARCHIVE_DIR / "memories",
        overrides_file=ARCHIVE_DIR / projects.OVERRIDES_NAME)
    return result


def _head_text(path: str) -> str:
    """Текст файла из последнего коммита (пустая строка, если его там нет)."""
    old = subprocess.run(["git", "-c", "core.quotepath=false", "show", f"HEAD:{path}"],
                         cwd=ARCHIVE_DIR, capture_output=True, text=True, encoding="utf-8")
    return old.stdout if old.returncode == 0 else ""


def _lines_added(old_body: str, new_body: str) -> int:
    diff = difflib.unified_diff(old_body.splitlines(), new_body.splitlines(), lineterm="", n=0)
    return sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))


def _chat_pairs(is_chat) -> tuple[dict[str, dict], dict[str, int]]:
    """Изменённые файлы чатов парами «было → стало». Чат узнаём по хвосту имени файла
    (первые 8 символов uuid): название и папка проекта могут меняться.
    Второе значение — число изменённых файлов в остальных папках верхнего уровня."""
    pairs: dict[str, dict] = {}
    other: dict[str, int] = {}
    for line in _git("status", "--porcelain", "-uall").splitlines():
        code, path = line[:2].strip(), line[3:]
        old_path = None
        if " -> " in path:  # переименование, если изменения уже в индексе
            old_path, path = (x.strip('"') for x in path.split(" -> ", 1))
        path = path.strip('"')
        parts = Path(path).parts
        if is_chat(parts):
            pair = pairs.setdefault(Path(path).stem.rsplit("_", 1)[-1], {})
            if code in ("??", "A"):
                pair["new"] = path
            elif code == "D":
                pair["old"] = path
            else:
                pair["new"], pair["old"] = path, old_path or path
        elif parts[0] not in DERIVED_DIRS:
            other[parts[0]] = other.get(parts[0], 0) + 1
    return pairs, other


def _versions(pair: dict) -> tuple[dict | None, dict | None]:
    was = render.meta_of(_head_text(pair["old"])) if "old" in pair else None
    now = (render.meta_of((ARCHIVE_DIR / pair["new"]).read_text(encoding="utf-8"))
           if "new" in pair else None)
    return was, now


def summarize_changes() -> dict:
    """Факты для сообщения коммита: какие чаты новые, продолжены, переименованы, удалены
    (по общему списку chats/) и какие перенесены в другой проект (по by_project/)."""
    def flat(parts):
        return (parts[0] == CHATS_DIR_NAME and parts[-1].endswith(".md")
                and parts[-1] != render.INDEX_NAME)

    def by_project(parts):
        return (parts[0] == BY_PROJECT_DIR_NAME and len(parts) == 4
                and parts[2] == render.CHATS_SUBDIR and parts[-1].endswith(".md"))

    pairs, other = _chat_pairs(flat)
    new, deleted, modified, renamed = [], [], [], []
    for pair in pairs.values():
        was, now = _versions(pair)
        if was is None:
            new.append(now["title"])
        elif now is None:
            deleted.append(was["title"] or pair["old"])
        else:
            if was["title"] != now["title"]:
                renamed.append({"from": was["title"], "to": now["title"]})
            if was["body"] != now["body"]:
                modified.append({"title": now["title"],
                                 "lines_added": _lines_added(was["body"], now["body"])})
    updated = sorted(modified, key=lambda x: -x["lines_added"])

    # Переносы между проектами видны только в раскладке по проектам. При первом её
    # появлении старых версий нет, поэтому переносов тоже нет — и это правда.
    moved, routes = [], {}
    for pair in _chat_pairs(by_project)[0].values():
        was, now = _versions(pair)
        if was and now and was["project"] != now["project"]:
            moved.append({"title": now["title"], "from_project": was["project"],
                          "to_project": now["project"]})
            key = f"{was['project']} → {now['project']}"
            routes[key] = routes.get(key, 0) + 1
    # Лимит защищает контекст от аномально больших списков; если он сработал —
    # говорим об этом явно, а не заставляем модель догадываться по цифрам.
    return {
        "new_chats": new[:LIST_LIMIT], "new_count": len(new),
        "new_chats_truncated": len(new) > LIST_LIMIT,
        "updated_chats": updated[:LIST_LIMIT], "updated_count": len(modified),
        "updated_chats_truncated": len(modified) > LIST_LIMIT,
        "renamed_chats": renamed, "deleted_chats": deleted,
        "moved_between_projects_count": len(moved),
        "moved_between_projects_routes": routes,
        "moved_between_projects": moved[:MOVED_LIMIT],
        "moved_between_projects_truncated": len(moved) > MOVED_LIMIT,
        "other_changed_files_by_folder": other,
    }


def _git(*args) -> str:
    r = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=ARCHIVE_DIR, capture_output=True,
                       text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def prepare_repo() -> dict:
    """Проверяет, что папка архива — git-репозиторий; клонирует при необходимости."""
    if (ARCHIVE_DIR / ".git").exists():
        return {"status": "exists", "remote": _git("remote", "-v").strip()}
    if not REPO_URL:
        raise RuntimeError("Папки архива нет, а REPO_URL в .env не задан.")
    if ARCHIVE_DIR.exists() and any(ARCHIVE_DIR.iterdir()):
        raise RuntimeError(f"{ARCHIVE_DIR} не пустая и не является git-репозиторием.")
    ARCHIVE_DIR.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["git", "clone", REPO_URL, str(ARCHIVE_DIR)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git clone: {r.stderr.strip()}")
    return {"status": "cloned", "repo": REPO_URL}


def git_status() -> dict:
    """Показывает, что изменится в репозитории, и предупреждает о слишком больших файлах."""
    lines = _git("status", "--porcelain", "-uall").splitlines()
    big = [str(p.relative_to(ARCHIVE_DIR)) for p in ARCHIVE_DIR.rglob("*")
           if p.is_file() and ".git" not in p.parts and p.stat().st_size > GITHUB_FILE_LIMIT]
    return {"changed_files": len(lines), "sample": lines[:40], "too_big_for_github": big}


def git_commit_and_push(message: str) -> dict:
    """Коммит и push. Подтверждение у человека запрашивает agent.py, не модель."""
    _git("add", "-A")
    if not _git("status", "--porcelain"):
        return {"status": "nothing_to_commit"}
    _git("commit", "-m", message)
    _git("push", "-u", "origin", "HEAD")
    return {"status": "pushed", "commit": _git("rev-parse", "--short", "HEAD").strip()}


# ---------- Проверки аргументов до исполнения ----------
# Формат, который можно проверить кодом, проверяем кодом, а не надеемся на промпт.
# Проверка вызывается в agent.py ДО запроса подтверждения у человека.
_EMPTY_SECTION = re.compile(
    r"^\s*[-*•]?\s*(?:[^:\n]{2,60}:\s*)?(нет|0|—|-|отсутствуют|пусто|none)\.?\s*$",
    re.IGNORECASE)


def check_commit_message(message: str) -> list[str]:
    """Возвращает список нарушений формата сообщения коммита (пустой — всё в порядке)."""
    problems = []
    lines = message.strip("\n").splitlines()
    first = lines[0] if lines else ""
    if not first.startswith("Claude export "):
        problems.append(f"первая строка должна начинаться с «Claude export », сейчас: «{first[:80]}»")
    if len(first) > 72:
        problems.append(f"первая строка длиннее 72 символов ({len(first)})")
    if len(lines) > 1 and lines[1].strip():
        problems.append("вторая строка должна быть пустой")
    empty = [ln.strip() for ln in lines[1:] if _EMPTY_SECTION.match(ln)]
    if empty:
        problems.append("пустые разделы не пиши, убери эти строки: " +
                        "; ".join(f"«{e}»" for e in empty[:5]))
    return problems


VALIDATORS = {"git_commit_and_push": check_commit_message}


# ---------- Описания для модели ----------
def _tool(fn, description, props=None, required=None):
    return {
        "name": fn.__name__,
        "description": description,
        "input_schema": {"type": "object", "properties": props or {},
                         "required": required or []},
    }

TOOLS = [
    _tool(find_export_emails,
          "Ищет в Gmail (INBOX) письма от Anthropic. Возвращает тему, время отправки, "
          "сколько минут назад пришло и ссылки экспорта claude.ai.",
          {"days": {"type": "integer", "description": "Глубина поиска в днях, по умолчанию 7"}}),
    _tool(wait_for_manifest,
          "Ищет манифест экспорта (manifest*.json) в папке Загрузки и ждёт его появления. "
          "Вызывай ДО открытия ссылки: пользователь мог уже скачать манифест сам.",
          {"max_age_minutes": {"type": "integer", "description": "Насколько свежим должен быть файл, по умолчанию 120"},
           "timeout_sec": {"type": "integer", "description": "Сколько ждать, по умолчанию 120; 0 — только проверить"}}),
    _tool(open_in_browser,
          "Открывает ссылку экспорта claude.ai в браузере пользователя, где он вошёл в аккаунт; "
          "браузер скачает файл в Загрузки. Каждую ссылку можно открыть только один раз.",
          {"url": {"type": "string"}}, ["url"]),
    _tool(read_manifest,
          "Читает манифест экспорта: список zip-файлов, возраст и дату экспорта по местному времени "
          "(export_date_local). Ссылки живут не более 24 часов.",
          {"path": {"type": "string"}}, ["path"]),
    _tool(fetch_export_files,
          "Получает все zip из манифеста через браузер пользователя: открывает ссылки "
          "(каждую один раз), ждёт файлы в Загрузках, проверяет и переносит в рабочую папку. "
          "Можно вызывать повторно: уже полученные файлы пропускаются.",
          {"manifest_path": {"type": "string"},
           "timeout_sec": {"type": "integer", "description": "Сколько ждать загрузок, по умолчанию 900"}},
          ["manifest_path"]),
    _tool(prepare_repo,
          "Проверяет или клонирует локальный репозиторий архива. Вызови перед распаковкой."),
    _tool(unpack_to_archive,
          "Распаковывает полученные zip в папку репозитория по категориям, сохраняет "
          "манифест без ссылок и копирует сами zip без изменений в archives/<время экспорта>/.",
          {"manifest_path": {"type": "string"}}, ["manifest_path"]),
    _tool(render_conversations,
          "Раскладывает переписку по Markdown-файлам: chats/ — все чаты одним списком с оглавлением; "
          "by_project/<проект>/ — чаты, документы и память по проектам. В by_project результата: "
          "число чатов по проектам, число чатов без проекта и ошибки в файле ручных привязок "
          "(override_errors). Вызывай после unpack_to_archive."),
    _tool(git_status, "Показывает изменения в репозитории перед коммитом."),
    _tool(summarize_changes,
          "Возвращает факты об изменениях в чатах: новые, продолженные (сколько строк добавлено), "
          "переименованные, перенесённые в другой проект, удалённые, плюс изменения в остальных "
          "папках. Используй для текста коммита."),
    _tool(git_commit_and_push,
          "Делает commit и push в приватный репозиторий. Человек подтвердит действие сам.",
          {"message": {"type": "string", "description": "Сообщение коммита: первая строка — краткий итог, затем пустая строка и подробности"}}, ["message"]),
]

HANDLERS = {t["name"]: globals()[t["name"]] for t in TOOLS}
