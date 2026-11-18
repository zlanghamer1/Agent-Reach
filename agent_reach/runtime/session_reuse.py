"""Site-scoped session reuse, dedicated browser only (fork).

This fork restores a dedicated-only policy: cookies are never read from the
user's own browsers. Only saved Reach state is reused; otherwise the user signs
in themselves in the dedicated window. No password/keychain API.
"""
from __future__ import annotations

import json
import time

from agent_reach.utils.paths import PrivatePathError, read_small_text_no_follow

from . import bridge, browser, cdp, state
from .browser_binding import wait_lock
from .result import NEED_SETUP, UPSTREAM_BROKEN, ReachError


def _cookies(rows, domains) -> list:
    out = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("name") or "value" not in row:
            continue
        if not any(cdp.domain_matches(str(row.get("domain", "")), domain) for domain in domains):
            continue
        expires = row.get("expires", row.get("expirationDate", -1))
        if not isinstance(expires, (int, float)) or (0 < expires <= time.time()):
            continue
        cookie = {k: row[k] for k in ("name", "value", "domain", "path", "secure", "httpOnly") if k in row}
        cookie["expires"] = expires
        same = {"no_restriction": "None", "lax": "Lax", "strict": "Strict",
                "None": "None", "Lax": "Lax", "Strict": "Strict"}.get(str(row.get("sameSite", "")))
        if same:
            cookie["sameSite"] = same
        out.append(cookie)
    return out


def profiles() -> list:
    return [{"id": context, "dedicated": context == browser.bound_context()}
            for context in browser.connected_contexts()]


NO_COPY_HINT = ("This fork never copies sign-ins from your own browser. "
                "Run agent-reach login <channel> and sign in yourself in the dedicated window.")


def use(profile: str) -> dict:
    # Fork: choosing a source browser to copy sign-ins from is disabled.
    raise ReachError(NEED_SETUP, "Copying sign-ins from another browser is disabled in this fork",
                     hint=NO_COPY_HINT)


def _source_cookies(channel: str, domains) -> tuple[list, str]:
    # Fork: never read cookies from the user's own browsers. Only a login the
    # user made inside Agent Reach's dedicated browser (saved Reach state) is used.
    # Older Reach versions stored the user's login in a per-site owned snapshot.
    source = state.root() / "sessions" / (channel + "-cloak") / "profile" / "reach-session.json"
    try:
        data = json.loads(read_small_text_no_follow(source, max_bytes=2 * 1024 * 1024) or "{}")
        cookies = _cookies(data.get("cookies", []), domains)
    except (OSError, ValueError, PrivatePathError):
        cookies = []
    return cookies, "saved-reach-session" if cookies else "saved-profile"


def prepare(channel: str, *, force: bool = False) -> dict:
    """Reuse an imported site session until explicitly refreshed; verify access separately."""
    from .manage import _channel, site_domains
    ch = _channel(channel)
    if not ch.login_url:
        return {"source": "not_required", "copied_cookies": 0}
    # One copy per site at a time; queries of other sites keep running.
    with wait_lock(f"session:{channel}"):
        browser.ensure_running()
        record = state.load().get("session_reuse", {}).get(channel, {})
        if not force and (record.get("blocked") or record.get("source") == "manual"
                          or (record.get("cookie_snapshot") == 1 and record.get("copied_cookies", 0) > 0)):
            return record
        cookies, source = _source_cookies(channel, site_domains(ch))
        # Source discovery tolerates a disconnected optional Bridge for native
        # CLI sessions. A browser-backed action still needs it before recording
        # discovery as a successful (possibly empty) site snapshot.
        bridge.require_running()
        if force and not cookies:
            # An explicit import that found no login must not report success.
            raise ReachError(NEED_SETUP, "No local browser login was found for this site; nothing was copied",
                             hint=f"Run agent-reach browser profiles to check the source browser, or agent-reach login {channel}.")
        if cookies:
            current = cdp.call("Storage.getCookies")
            if current is None:
                raise ReachError(UPSTREAM_BROKEN, "Could not inspect the saved site session; the original browser is unchanged")
            previous = _cookies(current.get("cookies", []), site_domains(ch))
            def key(cookie):
                return cookie.get("name"), cookie.get("domain"), cookie.get("path", "/")
            incoming = {key(c) for c in cookies}
            # The source is a site snapshot, not an overlay onto another account.
            # Expire destination-only cookies (including stale UI preferences),
            # preserving all other sites and every cookie supplied by the source.
            stale = [{**c, "value": "", "expires": 1} for c in previous if key(c) not in incoming]
            if cdp.call("Storage.setCookies", {"cookies": stale + cookies}) is None:
                old = {key(c) for c in previous}
                rollback = [{**c, "value": "", "expires": 1} for c in cookies if key(c) not in old] + previous
                cdp.call("Storage.setCookies", {"cookies": rollback})
                raise ReachError(UPSTREAM_BROKEN, "Could not apply the local site session; saved state retained and the original browser is unchanged")
            browser.save_session()
        result = {"source": source, "copied_cookies": len(cookies), "at": state.now(),
                  "verified": False, "source_changed": False, "cookie_snapshot": 1}
        state.mutate(lambda data: data.setdefault("session_reuse", {}).__setitem__(channel, result))
        return result
