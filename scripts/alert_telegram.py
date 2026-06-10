#!/usr/bin/env python3
"""Limon Ops — Telegram alert (canonical, cross-project).

Sends an alert to the Limon Ops Telegram bot (@LimonOpsAlertsBot).

Reads from env (or nearest .env file):
  TELEGRAM_BOT_TOKEN   required
  TELEGRAM_CHAT_ID     required (target chat — user, group, or channel)

CLI:
  python alert_telegram.py --project da-leads --level error \
      --title "Scraper broken" --message "nsw_planning_portal down 3 days"

  # Long body from stdin:
  cat report.txt | python alert_telegram.py --project da-leads \
      --level warn --title "Weekly digest"

Library:
  from alert_telegram import send_alert
  send_alert(project="da-leads", level="error",
             title="Scraper broken", message="...")

Design notes:
- Pure stdlib (urllib) — works in any project's venv with no extra install.
- Silent on non-fatal issues (bad token etc. print but exit 1; caller decides).
- Message formatting: Telegram MarkdownV2 is picky; we use plain text + fence
  for the body, which is robust and readable on mobile.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from zoneinfo import ZoneInfo  # py3.9+
    MEL_TZ = ZoneInfo("Australia/Melbourne")
except Exception:
    MEL_TZ = None

LEVEL_ICON = {
    "info": "ℹ️",
    "warn": "⚠️",
    "warning": "⚠️",
    "error": "🔴",
    "critical": "🚨",
    "ok": "✅",
    "recovery": "✅",
}

LEVEL_LABEL_ZH = {
    "info": "信息",
    "warn": "警告",
    "warning": "警告",
    "error": "错误",
    "critical": "严重",
    "ok": "正常",
    "recovery": "恢复",
}


def _load_env_file() -> None:
    """Walk up from CWD looking for a .env; load missing keys into os.environ.

    Does not overwrite existing env vars (so explicit env wins over .env).
    """
    cwd = Path.cwd().resolve()
    for d in [cwd, *cwd.parents]:
        p = d / ".env"
        if not p.is_file():
            continue
        try:
            for raw in p.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        except Exception:
            pass
        return


def _server_display() -> str:
    import socket
    override = os.environ.get("SERVER_NAME", "").strip()
    try:
        host = socket.gethostname().split(".")[0]
    except Exception:
        host = ""
    if override and override != host:
        return f"{override} ({host})" if host else override
    return override or host or "unknown"


def _format_message(
    project: str, level: str, title: str, message: str, extra: Optional[dict] = None
) -> str:
    key = level.lower()
    icon = LEVEL_ICON.get(key, "•")
    label = LEVEL_LABEL_ZH.get(key, level.upper())
    if MEL_TZ is not None:
        ts = datetime.now(MEL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    else:
        ts = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
    server = _server_display()

    header = f"{icon} {project}"
    if title:
        header = f"{icon} {project} · {title}"
    header = f"{header}｜{label}"

    parts = [header, ""]
    parts.append(f"🖥 服务器：{server}")
    parts.append(f"📂 项目：{project}")
    if extra:
        for k, v in extra.items():
            parts.append(f"{k}：{v}")
    parts.append(f"🕐 时间：{ts}")
    if message:
        parts.append("")
        parts.append("────── 详情 ──────")
        parts.append(message.strip())
    return "\n".join(parts)


def send_alert(
    project: str,
    level: str,
    title: str = "",
    message: str = "",
    extra: Optional[dict] = None,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    parse_mode: Optional[str] = None,
    timeout: float = 10.0,
) -> bool:
    """Send a single Telegram message. Returns True on success.

    Raises no exceptions for network failures — prints and returns False.
    """
    _load_env_file()
    token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(
            "[alert_telegram] missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID",
            file=sys.stderr,
        )
        return False

    text = _format_message(project, level, title, message, extra)
    # Telegram caps messages at 4096 chars; truncate conservatively.
    if len(text) > 4000:
        text = text[:3990] + "\n…[truncated]"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        # Markdown parse errors surface as HTTP 400 with
        # description "can't parse entities". Retry as plain text.
        if parse_mode and e.code == 400 and "parse" in body.lower():
            return send_alert(
                project, level, title, message, extra,
                bot_token=token, chat_id=chat,
                parse_mode=None, timeout=timeout,
            )
        print(f"[alert_telegram] HTTP {e.code}: {body}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[alert_telegram] send failed: {e}", file=sys.stderr)
        return False

    try:
        data = json.loads(body)
    except Exception:
        print(f"[alert_telegram] bad response: {body}", file=sys.stderr)
        return False
    if not data.get("ok"):
        print(f"[alert_telegram] API error: {body}", file=sys.stderr)
        return False
    return True


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Send a Telegram alert via @LimonOpsAlertsBot")
    p.add_argument("--project", required=True, help="Project name (da-leads, audrive, …)")
    p.add_argument(
        "--level",
        default="info",
        choices=list(LEVEL_ICON.keys()),
        help="Severity (info/warn/error/critical/ok)",
    )
    p.add_argument("--title", default="", help="Short headline")
    p.add_argument("--message", default="", help="Body (use - to read from stdin)")
    args = p.parse_args(argv)

    message = args.message
    if message == "-" or (not message and not sys.stdin.isatty()):
        message = sys.stdin.read()

    ok = send_alert(
        project=args.project, level=args.level, title=args.title, message=message
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
