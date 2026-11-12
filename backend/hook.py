#!/usr/bin/env python3
"""PreToolUse-хук Claude Code. Спрашивает у веб-сервера, можно ли выполнить инструмент.

Сервер может ответить сразу (allow/deny) или подождать, пока пользователь нажмёт
«Разрешить/Запретить» в интерфейсе. Если сервер недоступен — запрет (fail closed).
"""
import json
import os
import sys
import urllib.request


def out(decision: str, reason: str):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": reason}},
        ensure_ascii=False))
    sys.exit(0)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        out("deny", "webpanel: некорректный ввод хука")
    url = os.environ.get("WEBPANEL_HOOK_URL")
    token = os.environ.get("WEBPANEL_HOOK_TOKEN")
    chat = os.environ.get("WEBPANEL_CHAT_ID")
    if not (url and token and chat):
        out("deny", "webpanel: хук не настроен")
    body = json.dumps({"chat_id": chat, "tool_name": payload.get("tool_name"),
                       "tool_input": payload.get("tool_input"), "tool_use_id": payload.get("tool_use_id")}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Hook-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            res = json.load(r)
        out(res.get("decision", "deny"), res.get("reason", ""))
    except Exception as e:
        out("deny", f"webpanel: сервер политики недоступен ({e.__class__.__name__}), действие запрещено")


main()
