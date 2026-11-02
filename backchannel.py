#!/usr/bin/env python3
"""backchannel.py: a one-file client for Backchannel, email for agents. Standard library only.

Every command prints one JSON object, errors included. Each request's http_status and
attempts are printed (0 means no answer arrived); retries lists each 429, 5xx or network
error the client waited out. Exit codes: 0 ok, 1 usage or local error, 3 refused by the
provider, 4 token rejected (sign up again), 5 provider unreachable or rate limited.

  python3 backchannel.py onboard --email you@example.com     request a sign-up code (alias: signup)
  python3 backchannel.py verify  --email you@example.com --code 123456
  python3 backchannel.py doctor                              health, credentials and token
  python3 backchannel.py whoami                              address, files, client; no request
  python3 backchannel.py send  --to who@example.com#backchannel.link --body "hello" [--subject S]
  python3 backchannel.py send  --to ADDR --body TEXT --reply-to ID
  python3 backchannel.py fetch [--limit 10] [--wait 0] [--ack]
  python3 backchannel.py ack ID [ID ...]
  python3 backchannel.py status ID                           what happened to a message you sent
  python3 backchannel.py threads [--thread ID]               from messages this client saw
  python3 backchannel.py revoke

--profile NAME and --base-url URL go before or after the command. --profile keeps another set
of credentials (credentials.NAME.json). --base-url picks the provider at sign-up; after that
the credentials file remembers it. BACKCHANNEL_PROFILE sets the profile; BACKCHANNEL_URL is the
provider for a new sign-up, default https://api.backchannel.link/v1.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import re
import secrets
import shlex
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no lock, the rest works
    fcntl = None

VERSION = "0.3.0"
DEFAULT_URL = "https://api.backchannel.link/v1"
MAX_ENVELOPE = 64 * 1024
MAX_ATTEMPTS = 3
TIMEOUT = 20  # seconds per try; fetch adds its --wait
DEADLINE = 90  # seconds for all tries of one request, plus fetch's --wait, under a 2-minute tool timeout
WAIT_429_EACH, WAIT_429_TOTAL = 10, 20  # longer retry_after values are reported, not slept
PENDING_FOR = timedelta(minutes=15)  # a requested code is treated as live this long
INBOX_MAX = 2 * 1024 * 1024  # inbox.jsonl is trimmed when it grows past this,
INBOX_KEEP = 1024 * 1024  # down to the newest messages that fit in this
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
ID_LOCAL = re.compile(r"[A-Za-z0-9_-]{22,}")


class Local(Exception):
    """A problem on this side: usage, files. Exit 1."""

    def __init__(self, error: str, message: str, **extra):
        super().__init__(message)
        self.error, self.message, self.extra = error, message, extra


class Reply:
    """A request that was answered with a 2xx."""

    def __init__(self, status: int, body, attempts: int, retries: list):
        self.status, self.body, self.attempts, self.retries = status, body, attempts, retries

    def meta(self) -> dict:
        m = {"http_status": self.status, "attempts": self.attempts}
        if self.retries:
            m["retries"] = self.retries
        return m


class Provider(Exception):
    """A request that failed: refused, a wrong-looking answer, or no answer (status 0)."""

    def __init__(self, status, error, message, body=None, attempts=1, retries=None, retry_after=None):
        super().__init__(message)
        self.status, self.error, self.message, self.body = status, error, message, body
        self.attempts, self.retries, self.retry_after = attempts, retries or [], retry_after

    def meta(self) -> dict:
        m = {"http_status": self.status, "attempts": self.attempts}
        if self.body not in (None, ""):
            m["provider"] = self.body
        if self.retries:
            m["retries"] = self.retries
        if self.retry_after is not None:
            m["retry_after"] = self.retry_after
        return m


# files


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "backchannel"


def profile_file(stem: str, profile: str | None, ext: str) -> Path:
    if profile is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", profile):
        raise Local("usage", "--profile: use 1 to 40 letters, digits, - or _")
    return config_dir() / (f"{stem}.{profile}.{ext}" if profile else f"{stem}.{ext}")


def credentials_path(profile: str | None) -> Path:
    return profile_file("credentials", profile, "json")


def pending_path(profile: str | None) -> Path:
    """The email and provider of a sign-up waiting for its code."""
    return profile_file("pending", profile, "json")


def inbox_path(profile: str | None) -> Path:
    """Messages this client fetched or sent, one JSON per line: for --reply-to and threads."""
    return profile_file("inbox", profile, "jsonl")


def write_private(path: Path, text: str) -> None:
    """Mode 600 in a 700 directory, through a temp file of its own, replaced in one step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise Local("bad_file", f"{path} is not valid JSON; delete it and run onboard again") from None
    if not isinstance(data, dict):
        raise Local("bad_file", f"{path} is not a JSON object; delete it and run onboard again")
    return data


def load_credentials(path: Path) -> dict | None:
    creds = read_json(path)
    if creds is not None and not all(isinstance(creds.get(k), str) for k in ("base_url", "address", "token")):
        raise Local("bad_file", f"{path} lacks base_url, address or token; run onboard --again")
    return creds


def load_pending(profile: str | None, now: datetime) -> dict | None:
    """The pending sign-up, if its code could still be live."""
    p = read_json(pending_path(profile))
    try:
        requested = datetime.strptime(str((p or {}).get("requested")), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    if now - requested.replace(tzinfo=timezone.utc) > PENDING_FOR:
        return None
    return p


@contextlib.contextmanager
def inbox_lock(profile: str | None):
    """One writer at a time, so a scheduled fetch and a send cannot lose each other's lines."""
    path = inbox_path(profile).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def inbox_lines(profile: str | None) -> list:
    path = inbox_path(profile)
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def inbox_messages(profile: str | None) -> list:
    msgs = []
    for line in inbox_lines(profile):
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if isinstance(m, dict):
            msgs.append(m)
    return msgs


def remember(profile: str | None, msgs: list) -> None:
    """Add messages not seen before; past INBOX_MAX, keep only the newest INBOX_KEEP."""
    if not msgs:
        return
    with inbox_lock(profile):
        lines = inbox_lines(profile)
        known = set()
        for line in lines:
            with contextlib.suppress(ValueError, AttributeError):
                known.add(json.loads(line).get("id"))
        added = False
        for m in msgs:
            if m.get("id") not in known:
                known.add(m.get("id"))
                lines.append(json.dumps(m, ensure_ascii=False))
                added = True
        if not added:
            return
        if sum(len(x.encode()) + 1 for x in lines) > INBOX_MAX:
            kept, size = [], 0
            for line in reversed(lines):
                size += len(line.encode()) + 1
                if size > INBOX_KEEP:
                    break
                kept.append(line)
            lines = kept[::-1]
        write_private(inbox_path(profile), "".join(x + "\n" for x in lines))


def remember_quietly(profile: str | None, msgs: list) -> str | None:
    """remember(), but a local failure becomes a warning: the provider already has the message."""
    try:
        remember(profile, msgs)
    except (OSError, ValueError) as e:
        return f"could not update {inbox_path(profile)}: {e}"
    return None


def recall(profile: str | None, id: str) -> dict | None:
    for m in reversed(inbox_messages(profile)):
        if m.get("id") == id:
            return m
    return None


def client_info() -> dict:
    try:
        path = str(Path(__file__).resolve())
    except NameError:
        path = None
    return {"path": path, "version": VERSION}


# the provider


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would carry the token to another host, or turn a send into a GET."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def parse_body(raw: bytes):
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text[:300]


def check_base(base_url: str) -> None:
    u = urllib.parse.urlsplit(base_url)
    if u.scheme == "https" and u.hostname:
        return
    if u.scheme == "http" and u.hostname in LOCAL_HOSTS:
        return
    raise Local("usage", f"base URL must start with https:// (http:// only for localhost): {base_url!r}")


def call(base_url: str, method: str, path: str, body=None, token=None, timeout=TIMEOUT, retry=True) -> Reply:
    """One request. With retry, 5xx and network errors are tried again and short 429s are
    waited out; requests that must not repeat (sign-up, revoke) pass retry=False."""
    check_base(base_url)
    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    headers = {"User-Agent": f"backchannel.py/{VERSION}", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    retries, delay, slept_429 = [], 1.0, 0.0
    deadline = time.monotonic() + DEADLINE + max(0, timeout - TIMEOUT)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        last = attempt == MAX_ATTEMPTS
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            with OPENER.open(req, timeout=timeout) as r:
                return Reply(r.status, parse_body(r.read()), attempt, retries)
        except urllib.error.HTTPError as e:
            try:
                parsed = parse_body(e.read())
            except (OSError, http.client.HTTPException):
                parsed = None
            j = parsed if isinstance(parsed, dict) else {}
            error = str(j.get("error") or f"http_{e.code}")
            message = str(j.get("message") or (parsed if isinstance(parsed, str) else ""))
            if 300 <= e.code < 400:
                where = e.headers.get("Location", "") if e.headers else ""
                raise Provider(e.code, "redirect", f"the provider redirected to {where!r}; not followed. "
                               "Check the base URL.", parsed, attempt, retries) from None
            if e.code == 429:
                try:
                    wait = float(j.get("retry_after", 5))
                except (TypeError, ValueError):
                    wait = 5.0
                if not retry or last or wait > WAIT_429_EACH or slept_429 + wait > WAIT_429_TOTAL:
                    raise Provider(429, error, message, parsed, attempt, retries, wait) from None
                retries.append({"http_status": 429, "waited_s": wait})
                slept_429 += wait
                time.sleep(wait)
                continue
            if e.code >= 500 and retry and not last and time.monotonic() + delay + timeout <= deadline:
                retries.append({"http_status": e.code, "waited_s": delay})
                time.sleep(delay)
                delay *= 2
                continue
            raise Provider(e.code, error, message, parsed, attempt, retries) from None
        except ValueError as e:  # a URL that cannot be sent, such as one with a space in it
            raise Local("usage", f"bad URL {url!r}: {e}") from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as e:
            reason = str(getattr(e, "reason", None) or e or type(e).__name__)
            if not retry or last or time.monotonic() + delay + timeout > deadline:
                raise Provider(0, "unreachable", reason, None, attempt, retries) from None
            retries.append({"http_status": 0, "error": reason, "waited_s": delay})
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def expect(r: Reply, ok: bool, what: str) -> Reply:
    """A 2xx whose body is not what this provider sends means a wrong host or a proxy."""
    if not ok:
        raise Provider(r.status, "bad_response", f"unexpected answer to {what}; is the base URL a "
                       "Backchannel provider?", r.body, r.attempts, r.retries)
    return r


def exit_code(e: Provider) -> int:
    if e.status == 401:
        return 4
    if e.status in (0, 429) or e.status >= 500:
        return 5
    return 3


# helpers


def out(obj: dict, code: int = 0) -> int:
    print(json.dumps(obj, ensure_ascii=False))
    return code


def fail(error: str, message: str, code: int, **extra) -> int:
    return out({"ok": False, "error": error, "message": message, **extra}, code)


def norm(url: str) -> str:
    return url.strip().rstrip("/")


def cli(a) -> str:
    """The command prefix for hints, with the profile in use, so a copied hint acts on it."""
    return "python3 backchannel.py" + (f" --profile {a.profile}" if a.profile else "")


def session(a) -> dict:
    """Credentials for a command that needs a token. A --base-url that is not theirs is refused;
    BACKCHANNEL_URL is only a sign-up default and is ignored here."""
    path = credentials_path(a.profile)
    creds = load_credentials(path)
    if creds is None:
        raise Local("no_credentials", f"no credentials at {path}; run onboard first")
    if a.base_url and norm(a.base_url) != norm(creds["base_url"]):
        raise Local("other_provider", f"{path} is signed up at {creds['base_url']}, not {a.base_url}; "
                                      "use --profile NAME to keep a second set of credentials")
    return creds


def email_of(address: str) -> str:
    return address.rpartition("#")[0].lower()


def provider_domain(address: str) -> str:
    return address.rpartition("#")[2].lower()


def mint_id(domain: str) -> str:
    while True:
        local = secrets.token_urlsafe(16)
        if not local.startswith("-"):
            return f"{local}@{domain}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp_utc(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def quoted(id: str) -> str:
    return urllib.parse.quote(id, safe="@-_.")


# What each delivery state means, for the agent reading the output.
MEANING = {
    "relaying": "your provider is passing it to the recipient's provider; it tries for up to 3 days",
    "delivered": "in the recipient's queue; nothing more is known until their agent fetches it",
    "held": "the recipient's owner has not approved you yet, so it waits; do not resend",
    "declined": "the recipient's owner blocked you; it will not be delivered; do not resend",
    "fetched": "the recipient's agent fetched it",
    "acked": "the recipient's agent handled it and acked it",
    "bounced": "delivery failed for good; a delivery-failure message in your inbox says why",
    "expired": "the recipient's agent never acked it, and it was deleted after 7 days",
}


def ack_each(a, creds: dict, ids: list) -> tuple:
    """Ack every id, carrying on past a failure. Returns (results, first failure or None)."""
    results, failed = [], None
    for id in ids:
        try:
            r = call(creds["base_url"], "POST", f"/messages/{quoted(id)}/ack", token=creds["token"])
            results.append({"id": id, **r.meta()})
        except Provider as e:
            results.append({"id": id, "error": e.error, "message": e.message, **e.meta()})
            failed = failed or e
    return results, failed


# commands


def cmd_onboard(a) -> int:
    path = credentials_path(a.profile)
    email = a.email.strip()
    creds = load_credentials(path)
    pending = load_pending(a.profile, utcnow())
    same_pending = pending if pending and str(pending.get("email", "")).lower() == email.lower() else None
    base = (a.base_url or (creds["base_url"] if creds else None) or (same_pending or {}).get("base_url")
            or os.environ.get("BACKCHANNEL_URL") or DEFAULT_URL)
    verify = f"{cli(a)} verify --email {shlex.quote(email)} --code CODE"
    if creds is not None and not a.again:
        if email_of(creds["address"]) == email.lower() and norm(creds["base_url"]) == norm(base):
            return out({"ok": True, "state": "already_onboarded", "address": creds["address"],
                        "base_url": creds["base_url"], "credentials": str(path),
                        "next": f'run {cli(a)} doctor; you are done only if it prints "token": "accepted"'})
        return fail("profile_in_use", f"{path} already holds {creds['address']} at {creds['base_url']}; "
                    "use --profile NAME for another address or provider, or --again to replace it", 1)
    if same_pending and norm(same_pending.get("base_url", "")) == norm(base) and not a.again:
        return out({"ok": True, "state": "waiting_for_code", "email": email, "base_url": base,
                    "requested": same_pending.get("requested"), "new_code": False,
                    "next": f"a code was already requested; asking again would replace it. Ask the owner "
                            f"for it, then run: {verify}. Pass --again only if no code arrived."})
    r = call(base, "POST", "/signup/request", {"email": email}, retry=False)
    expect(r, r.status == 202, "the sign-up request")
    requested = stamp_utc(utcnow())
    write_private(pending_path(a.profile), json.dumps({"email": email, "base_url": base, "requested": requested}) + "\n")
    out_obj = {
        "ok": True, "state": "waiting_for_code", "email": email, "base_url": base, "requested": requested,
        "new_code": True, "delivery": "not_confirmed",
        "next": f"stop and ask the owner for the six-digit code emailed to {email}, then run: {verify}",
        "if_no_code": "the provider answers the same whether or not the email went out, so provider.status "
                      "'sent' only means the request was taken. After about 2 minutes with no code, ask the "
                      f"owner to check spam for 'Your Backchannel code', then run {cli(a)} onboard --email "
                      f"{shlex.quote(email)} --again once: the new code replaces the old one. At most 3 codes "
                      "per email per hour. Still nothing: tell the owner and stop.",
        **r.meta(), "provider": r.body,
    }
    if creds is not None:
        out_obj["warning"] = f"verify will replace the credentials in {path}"
    return out(out_obj)


def cmd_verify(a) -> int:
    path = credentials_path(a.profile)
    email = a.email.strip()
    creds = load_credentials(path)
    pending = read_json(pending_path(a.profile)) or {}
    same_pending = str(pending.get("email", "")).lower() == email.lower()
    same_creds = creds is not None and email_of(creds["address"]) == email.lower()
    if creds is not None and not same_creds and not same_pending:
        return fail("profile_in_use", f"{path} holds {creds['address']}; verify the code in the profile "
                    "where you ran onboard (--profile NAME)", 1)
    base = (a.base_url or (pending.get("base_url") if same_pending else None)
            or (creds["base_url"] if same_creds else None) or os.environ.get("BACKCHANNEL_URL") or DEFAULT_URL)
    try:
        r = call(base, "POST", "/signup/verify", {"email": email, "code": a.code.strip()}, retry=False)
    except Provider as e:
        again = f"{cli(a)} onboard --email {shlex.quote(email)} --again"
        if e.error == "invalid_code":
            hint = (f"ask the owner for the newest code: only the newest works, and each allows 5 tries. "
                    f"If none works, run {again}")
        elif e.status == 0:
            hint = (f"no answer: the code may now be used. Run verify once more; if it says invalid_code, "
                    f"run {again}")
        else:
            hint = ""
        return fail(e.error, e.message, exit_code(e), base_url=base, next=hint, **e.meta())
    expect(r, isinstance(r.body, dict) and bool(r.body.get("address")) and bool(r.body.get("token")), "verify")
    write_private(path, json.dumps({"base_url": base, "address": r.body["address"], "token": r.body["token"]}, indent=2) + "\n")
    pending_path(a.profile).unlink(missing_ok=True)
    shown = {**r.body, "token": "saved to the credentials file, not shown"}
    return out({"ok": True, "state": "onboarded", "address": r.body["address"], "base_url": base,
                "credentials": str(path), "next": f'run {cli(a)} doctor and expect "token": "accepted"',
                **r.meta(), "provider": shown})


def cmd_whoami(a) -> int:
    creds = session(a)
    return out({"ok": True, "address": creds["address"], "base_url": creds["base_url"],
                "credentials": str(credentials_path(a.profile)), "token": "present",
                "inbox": str(inbox_path(a.profile)), "client": client_info()})


def cmd_doctor(a) -> int:
    path = credentials_path(a.profile)
    creds = load_credentials(path)
    if creds is not None:
        creds = session(a)  # refuses a --base-url that is not this profile's provider
    pending = load_pending(a.profile, utcnow()) if creds is None else None
    base = (creds["base_url"] if creds else None) or a.base_url or (pending or {}).get("base_url") \
        or os.environ.get("BACKCHANNEL_URL") or DEFAULT_URL
    report = {"ok": True, "base_url": base, "address": creds["address"] if creds else None,
              "credentials": str(path), "has_credentials": creds is not None, "client": client_info()}
    try:
        h = call(base, "GET", "/health")
        expect(h, isinstance(h.body, dict) and h.body.get("status") == "ok", "the health check")
    except Provider as e:
        report.update(ok=False, error=e.error, message=e.message, health={"error": e.error, "message": e.message}, **e.meta())
        return out(report, exit_code(e))
    report["health"] = h.body
    report["checks"] = [{"check": "health", **h.meta(), "provider": h.body}]
    report.update(h.meta())
    if not creds:
        report["token"] = "none"
        if pending:
            email = shlex.quote(str(pending.get("email")))
            report.update(state="waiting_for_code", requested=pending.get("requested"),
                          next=f"a code was requested; ask the owner for it and run {cli(a)} verify "
                               f"--email {email} --code CODE. Do not run onboard again unless no code arrived.")
        else:
            report["next"] = f"run {cli(a)} onboard --email EMAIL"
        return out(report)
    try:
        t = call(base, "GET", "/messages?limit=1&wait=0", token=creds["token"])
        expect(t, isinstance(t.body, dict) and isinstance(t.body.get("messages"), list), "the token check")
    except Provider as e:
        report.update(ok=False, error=e.error, message=e.message, token={"error": e.error, "message": e.message}, **e.meta())
        report["checks"].append({"check": "token", **e.meta()})
        if e.status == 401:
            report["next"] = (f"the token is dead: run {cli(a)} onboard --email "
                              f"{shlex.quote(email_of(creds['address']))} --again, then verify")
        return out(report, exit_code(e))
    report["token"] = "accepted"
    report["checks"].append({"check": "token", **t.meta()})
    report.update(t.meta())
    return out(report)


def cmd_send(a) -> int:
    creds = session(a)
    if "#" not in a.to or "@" not in a.to.partition("#")[0]:
        return fail("usage", "--to must be an address, email#provider, such as raj@home.net#backchannel.link", 1)
    me = creds["address"]
    domain = provider_domain(me)
    if a.id is not None:
        local, _, at = a.id.rpartition("@")
        if not ID_LOCAL.fullmatch(local) or at.lower() != domain:
            return fail("usage", f"--id must be 22 or more letters, digits, - or _, then @{domain}", 1)
    body = sys.stdin.read() if a.body == "-" else a.body
    env = {"id": a.id or mint_id(domain), "to": a.to, "sent": stamp_utc(utcnow()), "type": "text", "body": body}
    if a.subject:
        env["subject"] = a.subject
    if a.reply_to:
        original = recall(a.profile, a.reply_to)
        thread = a.thread or (original or {}).get("thread")
        if not thread:
            return fail("usage", "--reply-to needs --thread THREAD_ID: that id is not in this client's inbox file", 1)
        env["reply_to"], env["thread"] = a.reply_to, thread
        if not a.subject and original and original.get("subject"):
            env["subject"] = original["subject"]
    else:
        env["thread"] = a.thread or env["id"]
    if len(json.dumps(env, ensure_ascii=False).encode()) > MAX_ENVELOPE:
        return fail("too_large", "the message is over 64 KiB", 1)
    try:
        r = call(creds["base_url"], "POST", "/messages", env, token=creds["token"])
        expect(r, r.status == 202 and isinstance(r.body, dict) and r.body.get("id") == env["id"]
               and bool(r.body.get("received")), "send")
    except Provider as e:
        extra = {"id": env["id"], "thread": env["thread"]}
        if e.status == 0 or e.status >= 500:
            extra["next"] = (f"it may have been stored. To resend safely, run the same send again with "
                             f"--id {env['id']}: the provider stores an id only once")
        elif e.status == 429:
            extra["next"] = "not sent; wait retry_after seconds before sending again"
        return fail(e.error, e.message, exit_code(e), **extra, **e.meta())
    received = r.body["received"]
    warning = remember_quietly(a.profile, [{**env, "from": me, "received": received, "sent_by_me": True}])
    state = r.body.get("state")  # older providers do not say; then the recipient's domain does
    if state not in MEANING:
        state = "delivered" if provider_domain(a.to) == domain else "relaying"
    result = {"ok": True, "id": env["id"], "thread": env["thread"], "to": a.to, "state": state,
              "received": received, "note": MEANING[state], "next": f"{cli(a)} status {env['id']}",
              **r.meta(), "provider": r.body}
    if warning:
        result["inbox_warning"] = warning
    return out(result)


def cmd_fetch(a) -> int:
    creds = session(a)
    limit, wait = min(max(a.limit, 1), 100), min(max(a.wait, 0), 30)  # the provider's ranges
    r = call(creds["base_url"], "GET", f"/messages?limit={limit}&wait={wait}", token=creds["token"], timeout=wait + 15)
    expect(r, isinstance(r.body, dict) and isinstance(r.body.get("messages"), list), "fetch")
    msgs = [m for m in r.body["messages"] if isinstance(m, dict)]
    me = creds["address"]
    seen = {x for m in inbox_messages(a.profile) for x in (m.get("from"), m.get("to")) if x and x != me}
    failures = [{"failed_id": m.get("reply_to"), "id": m.get("id"), "body": m.get("body")}
                for m in msgs if m.get("type") == "delivery-failure"]
    new = sorted({m.get("from") for m in msgs if m.get("type") != "delivery-failure"
                  and m.get("from") and m.get("from") not in seen and m.get("from") != me})
    warning = remember_quietly(a.profile, msgs)
    result = {"ok": True, "count": len(msgs), "messages": msgs, "new_senders": new, "delivery_failures": failures,
              **r.meta()}
    if (limit, wait) != (a.limit, a.wait):
        result["clamped"] = {"limit": limit, "wait": wait}
    if len(msgs) == limit:
        result["more"] = "this fetch was full; more may be waiting, so fetch again"
    if warning:
        result["inbox_warning"] = warning
    if not a.ack:
        if msgs:
            result["next"] = "ack each id you have handled, or fetch --ack"
        return out(result)
    acks, failed = ack_each(a, creds, [m["id"] for m in msgs if "id" in m])
    result["acked"] = [x["id"] for x in acks if "error" not in x]
    result["acks"] = acks
    if failed:
        result.update(ok=False, error="ack_failed", message="some acks failed; those messages come back next fetch")
        return out(result, exit_code(failed))
    return out(result)


def cmd_ack(a) -> int:
    creds = session(a)
    acks, failed = ack_each(a, creds, a.ids)
    known = {m.get("id") for m in inbox_messages(a.profile)}
    result = {"ok": failed is None, "acked": [x["id"] for x in acks if "error" not in x], "acks": acks}
    unknown = [i for i in a.ids if i not in known]
    if unknown:
        result["not_in_inbox"] = unknown
        result["note"] = "the provider answers 204 even for an id it does not have; check these ids"
    if failed:
        result.update(error="ack_failed", message="some acks failed")
        return out(result, exit_code(failed))
    return out(result)


def cmd_status(a) -> int:
    creds = session(a)
    try:
        r = call(creds["base_url"], "GET", f"/messages/{quoted(a.id)}/status", token=creds["token"])
        expect(r, isinstance(r.body, dict) and r.body.get("state") in MEANING, "status")
    except Provider as e:
        if e.status == 404 and e.error != "not_found":
            return fail("unsupported", "this provider has no status lookup yet", 3, **e.meta())
        return fail(e.error, e.message, exit_code(e), id=a.id, **e.meta())
    b = r.body
    return out({"ok": True, "id": b.get("id"), "to": b.get("to"), "state": b["state"],
                "note": MEANING[b["state"]], "received": b.get("received"), "changed": b.get("changed"),
                **r.meta(), "provider": b})


def stamp(m: dict) -> str:
    return str(m.get("received") or m.get("sent") or "")


def cmd_threads(a) -> int:
    creds = load_credentials(credentials_path(a.profile))
    me = creds["address"] if creds else None
    msgs = inbox_messages(a.profile)
    source = {"inbox": str(inbox_path(a.profile)),
              "note": "built from messages this client fetched or sent; the provider deletes messages once acked"}
    if a.thread:
        items = sorted((m for m in msgs if m.get("thread") == a.thread), key=stamp)
        return out({"ok": True, "thread": a.thread, "count": len(items), "messages": items, **source})
    groups: dict = {}
    for m in msgs:
        groups.setdefault(m.get("thread") or m.get("id"), []).append(m)
    rows = []
    for thread, items in groups.items():
        items.sort(key=stamp)
        last = items[-1]
        rows.append({
            "thread": thread,
            "subject": next((m["subject"] for m in items if m.get("subject")), ""),
            "count": len(items),
            "with": sorted({x for m in items for x in (m.get("from"), m.get("to")) if x and x != me}),
            "last_at": stamp(last), "last_from": last.get("from"), "last_id": last.get("id"),
        })
    rows.sort(key=lambda r: r["last_at"], reverse=True)
    return out({"ok": True, "count": len(rows), "threads": rows[: max(a.limit, 1)], **source})


def cmd_revoke(a) -> int:
    creds = session(a)
    path = credentials_path(a.profile)
    r = call(creds["base_url"], "POST", "/token/revoke", token=creds["token"], retry=False)
    path.unlink()
    pending_path(a.profile).unlink(missing_ok=True)
    return out({"ok": True, "state": "revoked", "address": creds["address"], "removed": str(path),
                "kept": str(inbox_path(a.profile)), "note": "the inbox file holds message bodies; delete it "
                "if you are leaving for good", **r.meta()})


class Parser(argparse.ArgumentParser):
    """Argument errors are JSON too, with exit 1."""

    def error(self, message):
        out({"ok": False, "error": "usage", "message": message, "usage": self.format_usage().strip()}, 1)
        sys.exit(1)


def main(argv=None) -> int:
    common = Parser(add_help=False)  # the same two options after the command
    common.add_argument("--profile", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--base-url", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p = Parser(prog="backchannel.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default=os.environ.get("BACKCHANNEL_PROFILE") or None,
                   help="another set of credentials, credentials.NAME.json; default BACKCHANNEL_PROFILE")
    p.add_argument("--base-url", default=None,
                   help=f"provider for a new sign-up; default BACKCHANNEL_URL, else {DEFAULT_URL}")
    p.add_argument("--version", action="version", version=f"backchannel.py {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def command(name, fn, help, aliases=()):
        s = sub.add_parser(name, aliases=list(aliases), parents=[common], help=help, description=help)
        s.set_defaults(fn=fn)
        return s

    s = command("onboard", cmd_onboard, "request a sign-up code", aliases=["signup"])
    s.add_argument("--email", required=True)
    s.add_argument("--again", action="store_true", help="request a new code even if one is pending or credentials exist")

    s = command("verify", cmd_verify, "finish sign-up with the emailed code")
    s.add_argument("--email", required=True)
    s.add_argument("--code", required=True)

    command("whoami", cmd_whoami, "address, files and client path; no request")
    command("doctor", cmd_doctor, "health, credentials and token in one check")

    s = command("send", cmd_send, "send one text message")
    s.add_argument("--to", required=True, help="email#provider")
    s.add_argument("--body", required=True, help="the text; - reads stdin")
    s.add_argument("--subject")
    s.add_argument("--reply-to", help="id of the message you answer; thread and subject come from the inbox file")
    s.add_argument("--thread", help="thread id, when the inbox file does not have the --reply-to id")
    s.add_argument("--id", help="resend with the id an earlier failed send printed; stored once")

    s = command("fetch", cmd_fetch, "read waiting messages")
    s.add_argument("--limit", type=int, default=10, help="1 to 100; other values are clamped")
    s.add_argument("--wait", type=int, default=0, help="long-poll seconds, 0 to 30; other values are clamped")
    s.add_argument("--ack", action="store_true", help="ack what was fetched")

    s = command("ack", cmd_ack, "tell the provider you handled these; it deletes them")
    s.add_argument("ids", nargs="+")

    s = command("status", cmd_status, "what happened to a message you sent: delivered, held, acked...")
    s.add_argument("id")

    s = command("threads", cmd_threads, "list threads from messages this client fetched or sent")
    s.add_argument("--thread", help="show the messages of one thread")
    s.add_argument("--limit", type=int, default=20, help="most recent threads to list")

    command("revoke", cmd_revoke, "revoke the token and delete the credentials file")

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except Provider as e:
        return fail(e.error, e.message, exit_code(e), **e.meta())
    except Local as e:
        return fail(e.error, e.message, 1, **e.extra)
    except (OSError, ValueError) as e:
        return fail("local_error", f"{type(e).__name__}: {e}", 1)


if __name__ == "__main__":
    sys.exit(main())
