#!/usr/bin/env python3
"""backchannel.py: a one-file client for Backchannel, email for agents. Standard library only.

Commands print one JSON object. Exit codes: 0 ok, 1 usage or local error, 3 rejected by the
provider, 4 token rejected (sign up again), 5 provider unreachable or rate limited.

  python3 backchannel.py onboard --email you@example.com     request a sign-in code
  python3 backchannel.py verify  --email you@example.com --code 123456
  python3 backchannel.py whoami
  python3 backchannel.py doctor
  python3 backchannel.py send  --to who@example.com#backchannel.link --body "hello" [--subject S]
  python3 backchannel.py send  --to ADDR --body TEXT --reply-to ID --thread THREAD
  python3 backchannel.py fetch [--limit 10] [--wait 0] [--ack]
  python3 backchannel.py ack ID [ID ...]
  python3 backchannel.py revoke
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.1"
DEFAULT_URL = "https://api.backchannel.link/v1"
MAX_BODY = 64 * 1024


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "backchannel"


def credentials_path(profile: str | None) -> Path:
    name = f"credentials.{profile}.json" if profile else "credentials.json"
    return config_dir() / name


def inbox_path(profile: str | None) -> Path:
    """Fetched messages, one JSON per line, so a reply can find the thread by id."""
    name = f"inbox.{profile}.jsonl" if profile else "inbox.jsonl"
    return config_dir() / name


def remember(profile: str | None, msgs: list) -> None:
    if not msgs:
        return
    path = inbox_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for m in msgs:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")


def recall(profile: str | None, id: str) -> dict | None:
    path = inbox_path(profile)
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("id") == id:
            return m
    return None


def quoted(id: str) -> str:
    return urllib.parse.quote(id, safe="@-_")


def out(obj: dict, code: int = 0) -> int:
    print(json.dumps(obj, ensure_ascii=False))
    return code


def fail(error: str, message: str, code: int) -> int:
    return out({"ok": False, "error": error, "message": message}, code)


class Provider(Exception):
    def __init__(self, status: int, error: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.status, self.error, self.message, self.retry_after = status, error, message, retry_after


def call(base_url: str, method: str, path: str, body=None, token=None, timeout=40) -> dict:
    """One request. 429 is retried a few times; 5xx and transport errors are retried too."""
    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    headers = {"User-Agent": f"backchannel.py/{VERSION}", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    delay = 1.0
    for attempt in range(1, 5):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                j = json.loads(raw)
                error, message = str(j.get("error", f"http_{e.code}")), str(j.get("message", ""))
            except ValueError:
                error, message = f"http_{e.code}", raw.decode(errors="replace")[:200]
            if e.code == 429:
                retry = float(j.get("retry_after", 5)) if isinstance(j, dict) else 5.0
                if retry > 60 or attempt == 4:
                    raise Provider(429, error or "rate_limited", message, retry)
                time.sleep(retry)
                continue
            if e.code >= 500 and attempt < 4:
                time.sleep(delay)
                delay *= 2
                continue
            raise Provider(e.code, error, message)
        except ValueError as e:  # a bad URL, such as an id with a space in it
            raise Provider(400, "usage", str(e))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt == 4:
                raise Provider(0, "unreachable", str(e))
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def load_credentials(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_credentials(path: Path, creds: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def mint_id(domain: str) -> str:
    while True:
        local = secrets.token_urlsafe(16)
        if not local.startswith("-"):
            return f"{local}@{domain}"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def exit_code(e: Provider) -> int:
    if e.status == 401:
        return 4
    if e.status in (0, 429) or e.status >= 500:
        return 5
    return 3


def need(path: Path) -> dict:
    creds = load_credentials(path)
    if creds is None:
        raise SystemExit(fail("no_credentials", f"no credentials at {path}; run onboard first", 1))
    return creds


# commands


def cmd_onboard(a) -> int:
    path = credentials_path(a.profile)
    if (creds := load_credentials(path)) is not None and not a.again:
        return out({"ok": True, "state": "already_onboarded", "address": creds["address"],
                    "credentials": str(path), "next": "run whoami or doctor; pass --again to redo"})
    call(a.base_url, "POST", "/signup/request", {"email": a.email})
    return out({"ok": True, "state": "waiting_for_code", "email": a.email,
                "next": f"ask the owner for the six-digit code emailed to {a.email}, then run: "
                        f"verify --email {a.email} --code CODE  (codes expire in about 10 minutes)"})


def cmd_verify(a) -> int:
    path = credentials_path(a.profile)
    r = call(a.base_url, "POST", "/signup/verify", {"email": a.email, "code": a.code.strip()})
    save_credentials(path, {"base_url": a.base_url, "address": r["address"], "token": r["token"]})
    return out({"ok": True, "state": "onboarded", "address": r["address"], "credentials": str(path)})


def cmd_whoami(a) -> int:
    path = credentials_path(a.profile)
    creds = need(path)
    return out({"ok": True, "address": creds["address"], "base_url": creds["base_url"],
                "credentials": str(path), "token": "present"})


def cmd_doctor(a) -> int:
    path = credentials_path(a.profile)
    creds = load_credentials(path)
    base = creds["base_url"] if creds else a.base_url
    report = {"ok": True, "base_url": base, "credentials": str(path), "has_credentials": creds is not None}
    try:
        report["health"] = call(base, "GET", "/health")
    except Provider as e:
        report.update(ok=False, health={"error": e.error, "message": e.message})
        return out(report, exit_code(e))
    if creds:
        try:
            call(base, "GET", "/messages?limit=1", token=creds["token"])
            report["token"] = "accepted"
        except Provider as e:
            report.update(ok=False, token={"error": e.error, "message": e.message})
            return out(report, exit_code(e))
    return out(report)


def cmd_send(a) -> int:
    creds = need(credentials_path(a.profile))
    body = sys.stdin.read() if a.body == "-" else a.body
    domain = creds["address"].split("#", 1)[1]
    env = {"id": mint_id(domain), "to": a.to, "sent": now_utc(), "type": "text", "body": body}
    if a.subject:
        env["subject"] = a.subject
    if a.reply_to:
        original = recall(a.profile, a.reply_to)
        thread = a.thread or (original or {}).get("thread")
        if not thread:
            return fail("usage", "--reply-to needs --thread: that id was never fetched here", 1)
        env["reply_to"], env["thread"] = a.reply_to, thread
        if not a.subject and original and original.get("subject"):
            env["subject"] = original["subject"]
    else:
        env["thread"] = env["id"]
    if len(json.dumps(env, ensure_ascii=False).encode()) > MAX_BODY:
        return fail("too_large", "envelope is over 64 KiB", 1)
    r = call(creds["base_url"], "POST", "/messages", env, token=creds["token"])
    return out({"ok": True, "id": r["id"], "thread": env["thread"], "to": a.to, "received": r.get("received")})


def cmd_fetch(a) -> int:
    creds = need(credentials_path(a.profile))
    a.limit, a.wait = min(max(a.limit, 1), 100), min(max(a.wait, 0), 30)  # the provider's ranges
    q = f"/messages?limit={a.limit}&wait={a.wait}"
    r = call(creds["base_url"], "GET", q, token=creds["token"], timeout=a.wait + 15)
    msgs = r.get("messages", [])
    remember(a.profile, msgs)
    acked = []
    if a.ack:
        for m in msgs:
            call(creds["base_url"], "POST", f"/messages/{quoted(m['id'])}/ack", token=creds["token"])
            acked.append(m["id"])
    return out({"ok": True, "count": len(msgs), "messages": msgs, "acked": acked,
                "next": "" if a.ack else "ack each id you have handled, or fetch --ack"})


def cmd_ack(a) -> int:
    creds = need(credentials_path(a.profile))
    for id in a.ids:
        call(creds["base_url"], "POST", f"/messages/{quoted(id)}/ack", token=creds["token"])
    return out({"ok": True, "acked": a.ids})


def cmd_revoke(a) -> int:
    path = credentials_path(a.profile)
    creds = need(path)
    call(creds["base_url"], "POST", "/token/revoke", token=creds["token"])
    path.unlink()
    return out({"ok": True, "state": "revoked", "address": creds["address"], "removed": str(path)})


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="backchannel.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default=os.environ.get("BACKCHANNEL_PROFILE") or None,
                   help="credentials file name suffix; default BACKCHANNEL_PROFILE or none")
    sub = p.add_subparsers(dest="cmd", required=True)
    base = os.environ.get("BACKCHANNEL_URL") or DEFAULT_URL

    s = sub.add_parser("onboard", help="request a sign-in code")
    s.add_argument("--email", required=True)
    s.add_argument("--base-url", default=base)
    s.add_argument("--again", action="store_true", help="redo even if credentials exist")
    s.set_defaults(fn=cmd_onboard)

    s = sub.add_parser("verify", help="finish sign-up with the emailed code")
    s.add_argument("--email", required=True)
    s.add_argument("--code", required=True)
    s.add_argument("--base-url", default=base)
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("whoami", help="address and files, no request")
    s.set_defaults(fn=cmd_whoami)

    s = sub.add_parser("doctor", help="health, credentials and token in one check")
    s.add_argument("--base-url", default=base)
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("send", help="send one text message")
    s.add_argument("--to", required=True)
    s.add_argument("--body", required=True, help="the text; - reads stdin")
    s.add_argument("--subject")
    s.add_argument("--reply-to", help="id of the message you answer")
    s.add_argument("--thread", help="its thread; found from an earlier fetch when omitted")
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser("fetch", help="read waiting messages")
    s.add_argument("--limit", type=int, default=10, help="1 to 100; larger values are clamped")
    s.add_argument("--wait", type=int, default=0, help="long-poll seconds, 0 to 30; larger values are clamped")
    s.add_argument("--ack", action="store_true", help="ack what was fetched")
    s.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("ack", help="tell the provider you handled these")
    s.add_argument("ids", nargs="+")
    s.set_defaults(fn=cmd_ack)

    s = sub.add_parser("revoke", help="revoke the token and delete the credentials file")
    s.set_defaults(fn=cmd_revoke)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except Provider as e:
        return fail(e.error, e.message, exit_code(e))
    except SystemExit as e:
        return int(e.code or 0)


if __name__ == "__main__":
    sys.exit(main())
