"""Проверка настроек перед первым запуском агента. Секреты не выводятся."""
import imaplib
import os
import subprocess

from dotenv import load_dotenv

load_dotenv()


def check(name, fn):
    try:
        print(f"✅ {name}: {fn()}")
    except Exception as e:
        print(f"❌ {name}: {type(e).__name__}: {e}")


def env():
    need = ["ANTHROPIC_API_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "REPO_URL", "ARCHIVE_DIR"]
    missing = [k for k in need if not os.getenv(k) or "..." in os.getenv(k) or "<" in os.getenv(k)
               or os.getenv(k).startswith(("you@", "xxxx"))]
    if missing:
        raise ValueError("не заполнены: " + ", ".join(missing))
    return "все переменные заполнены"


def api():
    import anthropic
    model = os.getenv("MODEL", "claude-sonnet-5-5")
    r = anthropic.Anthropic().messages.create(
        model=model, max_tokens=10, messages=[{"role": "user", "content": "Ответь словом: ок"}])
    return f"модель {model} отвечает: {r.content[0].text.strip()}"


def gmail():
    m = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
    m.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"].replace(" ", ""))
    m.select("INBOX", readonly=True)
    q = os.getenv("EXPORT_MAIL_QUERY", "(from:anthropic.com OR from:claude.ai)")
    _, data = m.uid("SEARCH", "X-GM-RAW", f'"newer_than:30d {q}"')
    m.logout()
    return f"вход выполнен, писем от Anthropic за 30 дней: {len(data[0].split())}"


def github():
    r = subprocess.run(["git", "ls-remote", os.environ["REPO_URL"]],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return "доступ к репозиторию есть"


if __name__ == "__main__":
    check("Файл .env", env)
    check("Claude API", api)
    check("Gmail", gmail)
    check("GitHub", github)
