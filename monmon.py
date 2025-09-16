#!/usr/bin/env python3


import os
import sys
import json
import time
import uuid
import queue
import difflib
import threading
import hashlib
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List
from sys import stdout
import requests
from bs4 import BeautifulSoup

# Optional Rich UI
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    from rich.prompt import Prompt
    from rich.text import Text
    _rich_available = True
    _console = Console()
except Exception:
    _rich_available = False
    _console = None

# UI width helper (keep things compact)
def _ui_width() -> int:
    try:
        if _rich_available and _console is not None:
            w = getattr(_console, "width", None)
            if not w:
                size = getattr(_console, "size", None)
                w = getattr(size, "width", 80) if size else 80
            # Cap for compact UI; ensure sensible min
            return max(60, min(96, int(w) - 2))
    except Exception:
        pass
    return 80

try:
    import importlib
    _playwright_available = importlib.util.find_spec("playwright") is not None
except Exception:
    _playwright_available = False

APP_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    import __main__
    setattr(__main__, "APP_DIR", APP_DIR)
except Exception:
    pass
    
APP_STATE_FILE = os.path.join(APP_DIR, "monitor_state.json")

STATE_LOCK = threading.RLock()
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win32; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"

# ========================
# Terminal color theme 
# ========================

RED   = "\033[91m"
YELLOW= "\033[93m"
GREEN = "\033[92m"                 
CYAN  = "\033[38;2;150;82;214m"   # royal purple (headers/info)
RESET = "\033[0m"

# common real-browsers UA presets
PRESET_USER_AGENTS: Dict[str, str] = {
    "Windows 11 · Chrome 127 (Default)": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Windows 11 · Firefox 128": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "macOS 14 · Safari 17": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "macOS 14 · Chrome 127": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Ubuntu · Chrome 127": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Ubuntu · Firefox 128": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Android 14 · Chrome Mobile": "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36",
    "iPhone iOS 17 · Safari": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "iPad iPadOS 17 · Safari": "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Edge on Windows 11": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0",
    "Truthful Requests (Python)": "python-requests/2.x",
}

# ========================
# Data structures
# ========================

@dataclass
class Monitor:
    id: str
    url: str
    selector: Optional[str]          # None means whole page
    mode: str                        # "html" or "text"
    interval_sec: int
    headers: Dict[str, str]
    last_hash: Optional[str]
    last_excerpt: Optional[str]
    etag: Optional[str]
    last_modified: Optional[str]
    use_js: bool = False
    js_wait_ms: int = 3000
    js_wait_selector: Optional[str] = None

    # sticky status about last fetch outcome
    last_state: Optional[str] = None    # one of: "ok", "http_error", "net_error"
    last_code: Optional[str] = None     # e.g. "200", "404", "gaierror", "ConnectionError"
    last_sig: Optional[str] = None      # stable signature of the error detail (hash)

    # Content-only changes option and empty-state memory
    content_only_changes: bool = False  # if True, ignore transitions to empty/missing content
    was_empty: Optional[bool] = None    # remembers if last *observed* content was empty when content_only_changes is True

@dataclass
class AppState:
    webhook_url: Optional[str]
    monitors: List["Monitor"]
    verbose_status: bool = False
    default_user_agent: Optional[str] = None
    deleted_ids: List[str] = None  # will be normalized to [] in load_state

# ========================
# Persistence
# ========================

def load_state() -> AppState:
    global DEFAULT_USER_AGENT
    if not os.path.exists(APP_STATE_FILE):
        return AppState(webhook_url=None, monitors=[], deleted_ids=[])

    try:
        with STATE_LOCK:
            with open(APP_STATE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)

        deleted_ids = list(raw.get("deleted_ids", []) or [])
        monitors: List[Monitor] = []
        for m in raw.get("monitors", []):
            mid = m.get("id") or str(uuid.uuid4())
            # skip tombstoned monitors
            if mid in deleted_ids:
                continue
            monitors.append(Monitor(
                id=mid,
                url=m["url"],
                selector=m.get("selector"),
                mode=m.get("mode", "text"),
                interval_sec=int(m.get("interval_sec", 60)),
                headers=m.get("headers", {}),
                last_hash=m.get("last_hash"),
                last_excerpt=m.get("last_excerpt"),
                etag=m.get("etag"),
                last_modified=m.get("last_modified"),
                use_js=bool(m.get("use_js", False)),
                js_wait_ms=int(m.get("js_wait_ms", 3000)),
                js_wait_selector=m.get("js_wait_selector"),
                last_state=m.get("last_state"),
                last_code=m.get("last_code"),
                last_sig=m.get("last_sig"),
                content_only_changes=bool(m.get("content_only_changes", False)),
                was_empty=m.get("was_empty"),
            ))

        webhook = raw.get("webhook_url")
        verbose_status = bool(raw.get("verbose_status", False))
        default_ua = raw.get("default_user_agent")
        if default_ua:
            DEFAULT_USER_AGENT = default_ua

        return AppState(
            webhook_url=webhook,
            monitors=monitors,
            verbose_status=verbose_status,
            default_user_agent=default_ua,
            deleted_ids=deleted_ids,            # 
        )
    except Exception as e:
        print(f"{YELLOW}Warning: failed to read state: {e}. Starting fresh.{RESET}")
        return AppState(webhook_url=None, monitors=[], deleted_ids=[])
        
# robust Windows-friendly atomic save with retries
def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    # protect with a process-wide lock to avoid concurrent clobbering
    with STATE_LOCK:
        attempts = 6
        delay = 0.2
        last_err = None
        for _ in range(attempts):
            tmp = f"{path}.{uuid.uuid4().hex}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
                return
            except Exception as e:
                last_err = e
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass
                time.sleep(delay)
                delay *= 1.5
        raise RuntimeError(f"Failed to save state after retries: {last_err}")


def save_state(state: AppState) -> None:
    # ensure the list exists
    if state.deleted_ids is None:
        state.deleted_ids = []

    # filter out any tombstoned monitors before writing
    live_monitors = [m for m in state.monitors if m.id not in state.deleted_ids]

    data = {
        "webhook_url": state.webhook_url,
        "verbose_status": state.verbose_status,
        "default_user_agent": state.default_user_agent,
        "deleted_ids": list(state.deleted_ids),           # 
        "monitors": [asdict(m) for m in live_monitors],   # 
    }
    _atomic_write_json(APP_STATE_FILE, data)

    try:
        with STATE_LOCK:
            with open(APP_STATE_FILE, "r", encoding="utf-8") as f:
                _ = json.load(f)
    except Exception:
        pass
      
# Safely persist only one monitor's fields by id without rewriting the list from a stale snapshot.
def save_monitor_delta(m: Monitor) -> None:
    with STATE_LOCK:
        try:
            if not os.path.exists(APP_STATE_FILE):
                return
            with open(APP_STATE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)

            deleted_ids = set(raw.get("deleted_ids", []) or [])
            if m.id in deleted_ids:
                # this monitor was deleted; never resurrect it
                return

            arr = raw.get("monitors", [])
            idx = None
            for i, itm in enumerate(arr):
                if itm.get("id") == m.id:
                    idx = i
                    break
            if idx is None:
                # not present; do not recreate
                return

            arr[idx] = asdict(m)
            raw["monitors"] = arr
            raw["deleted_ids"] = list(deleted_ids)  # unchanged; keep persisted

            _atomic_write_json(APP_STATE_FILE, raw)
        except Exception:
            pass


# keep in-memory state synced with disk without replacing the object reference
def reload_state_into(state: AppState) -> None:
    with STATE_LOCK:
        if not os.path.exists(APP_STATE_FILE):
            return
        try:
            with open(APP_STATE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return

        deleted_ids = list(raw.get("deleted_ids", []) or [])   # 
        new_monitors: List[Monitor] = []
        for m in raw.get("monitors", []):
            mid = m.get("id") or str(uuid.uuid4())
            if mid in deleted_ids:                              # 
                continue
            new_monitors.append(Monitor(
                id=mid,
                url=m["url"],
                selector=m.get("selector"),
                mode=m.get("mode", "text"),
                interval_sec=int(m.get("interval_sec", 60)),
                headers=m.get("headers", {}),
                last_hash=m.get("last_hash"),
                last_excerpt=m.get("last_excerpt"),
                etag=m.get("etag"),
                last_modified=m.get("last_modified"),
                use_js=bool(m.get("use_js", False)),
                js_wait_ms=int(m.get("js_wait_ms", 3000)),
                js_wait_selector=m.get("js_wait_selector"),
                last_state=m.get("last_state"),
                last_code=m.get("last_code"),
                last_sig=m.get("last_sig"),
                content_only_changes=bool(m.get("content_only_changes", False)),
                was_empty=m.get("was_empty"),
            ))

        state.monitors.clear()
        state.monitors.extend(new_monitors)
        state.webhook_url = raw.get("webhook_url")
        state.verbose_status = bool(raw.get("verbose_status", False))
        state.default_user_agent = raw.get("default_user_agent")
        state.deleted_ids = deleted_ids                         # 

# ========================
# HTTP/Browser fetching & content extraction
# ========================

def fetch_url(url: str, etag: Optional[str], last_modified: Optional[str], extra_headers: Dict[str, str]) -> requests.Response:
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    headers.update(extra_headers or {})
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    resp = requests.get(url, headers=headers, timeout=30)
    return resp
    
def fetch_rendered_html(url: str, wait_ms: int, wait_selector: Optional[str]) -> str:

    if not _playwright_available:
        raise RuntimeError("Playwright not installed. Run: pip install playwright && python -m playwright install chromium")

    from playwright.sync_api import sync_playwright  # type: ignore

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(user_agent=DEFAULT_USER_AGENT)
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # brief network-idle settle to avoid hydration flip-flops
            try:
                page.wait_for_load_state("networkidle", timeout=min(1500, max(1, wait_ms)))
            except Exception:
                pass

            # normalize the wait_for selector
            norm_wait = _normalize_selector_input(wait_selector) if wait_selector else None
            if norm_wait:
                try:
                    page.wait_for_selector(norm_wait, timeout=max(1, wait_ms))
                    # tiny settle after the node appears
                    try:
                        page.wait_for_timeout(250)
                    except Exception:
                        pass
                except Exception:
                    pass
            else:
                time.sleep(wait_ms / 1000.0)

            html = page.content()
        finally:
            browser.close()
    return html

def fetch_rendered_content(url: str, selector: str, mode: str, wait_ms: int = 1500, polls: int = 3, poll_interval_ms: int = 400) -> Optional[str]:
    """Render a page with Playwright and return stable content for selector.
    Returns None if the selection is unstable across quick polls (so caller should ignore).
    Uses a persistent browser profile per domain to keep cookies/AB-bucketing stable.
    """
    if not _playwright_available:
        raise RuntimeError("Playwright not installed. Run: pip install playwright && python -m playwright install chromium")

    from urllib.parse import urlparse
    from pathlib import Path
    from playwright.sync_api import sync_playwright  # type: ignore

    sel = _normalize_selector_input(selector) if selector else None

    # persistent profile per domain → consistent AB/cookie state across runs
    netloc = urlparse(url).netloc or "default"
    base_profile = Path(getattr(sys.modules.get("__main__"), "APP_DIR", os.getcwd())) / ".pw_profiles"
    profile_dir = base_profile / netloc
    profile_dir.mkdir(parents=True, exist_ok=True)

    def _read_once(page):
        if not sel:
            return (page.content() or "").strip()
        try:
            if mode == "text":
                val = page.eval_on_selector(sel, "el => (el.innerText||'').trim()")
            else:
                val = page.eval_on_selector(sel, "el => el.outerHTML")
            return (val or "").strip()
        except Exception:
            return "__MISSING__"  # sentinel to distinguish from empty string

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=True,
            viewport={"width": 1366, "height": 900},
            user_agent=DEFAULT_USER_AGENT,
        )
        try:
            ctx.set_extra_http_headers({
                "Accept-Language": "en-US,en;q=0.9",
                "DNT": "1",
                "Upgrade-Insecure-Requests": "1",
            })
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # settle network & hydrate
            try:
                page.wait_for_load_state("networkidle", timeout=min(2000, max(1, wait_ms)))
            except Exception:
                pass

            # wait for selector if provided
            if sel:
                try:
                    page.wait_for_selector(sel, timeout=max(1, wait_ms))
                    page.wait_for_timeout(250)
                except Exception:
                    pass

            readings = []
            for _ in range(max(1, polls)):
                readings.append(_read_once(page))
                if poll_interval_ms > 0:
                    try:
                        page.wait_for_timeout(poll_interval_ms)
                    except Exception:
                        time.sleep(poll_interval_ms / 1000.0)

            # stable if all equal
            if len(set(readings)) == 1:
                val = readings[0]
                return "" if val == "__MISSING__" else val
            else:
                # unstable → ignore this cycle
                return None
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def extract_content_from_html(html_text: str, selector: Optional[str], mode: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    if selector:
        # normalize selector before soup.select
        norm_selector = _normalize_selector_input(selector)
        nodes = soup.select(norm_selector)
        if not nodes:
            return ""
        if mode == "text":
            texts = [n.get_text(" ", strip=True) for n in nodes]
            return "\n".join(texts)
        else:
            htmls = [str(n) for n in nodes]
            return "\n".join(htmls)
    else:
        if mode == "text":
            return soup.get_text(" ", strip=True)
        else:
            return str(soup)


def extract_content(resp_text: str, selector: Optional[str], mode: str) -> str:
    return extract_content_from_html(resp_text, selector, mode)

def compute_hash(s: str) -> str:
    h = hashlib.sha256()
    h.update(s.encode("utf-8", errors="ignore"))
    return h.hexdigest()

def diff_excerpt(old: str, new: str, limit_lines: int = 40) -> str:
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    diff_lines = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))
    if len(diff_lines) > limit_lines:
        head = diff_lines[:limit_lines//2]
        tail = diff_lines[-limit_lines//2:]
        diff_lines = head + ["... (diff truncated) ..."] + tail
    return "\n".join(diff_lines) if diff_lines else "(content changed, but diff is empty)"

# colorize unified diff for console (red - / green + / purple headers)
def colorize_diff(diff_text: str) -> str:
    lines = diff_text.splitlines()
    colored = []
    for ln in lines:
        if ln.startswith('+++') or ln.startswith('---') or ln.startswith('@@'):
            colored.append(CYAN + ln + RESET)
        elif ln.startswith('+') and not ln.startswith('+++'):
            colored.append(GREEN + ln + RESET)
        elif ln.startswith('-') and not ln.startswith('---'):
            colored.append(RED + ln + RESET)
        else:
            colored.append(ln)
    return "\n".join(colored)

# ========================
# Discord notifications
# ========================

def send_discord(webhook_url: str, title: str, url: str, selector: Optional[str], mode: str, diff_text: str, footer_text: Optional[str] = None) -> None:
    if not webhook_url:
        print(f"{YELLOW}No Discord webhook set; skipping webhook alert.{RESET}")
        return
    max_len = 1700
    if len(diff_text) > max_len:
        os.makedirs("diffs", exist_ok=True)
        fname = f"{int(time.time())}_{uuid.uuid4().hex}.diff.txt"
        fpath = os.path.join("diffs", fname)
        try:
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(diff_text)
            diff_text = diff_text[:max_len] + f"\n... (truncated, full saved to {fpath}) ..."
        except Exception:
            diff_text = diff_text[:max_len] + "\n... (truncated) ..."
    embed = {
        "title": title,
        "url": url,
        "description": f"URL: {url}\nSelector: `{selector or '(whole page)'}`\nMode: `{mode}`\n\n```diff\n{diff_text}\n```"
    }
    if footer_text:
        embed["footer"] = {"text": footer_text}
    payload = {"content": None, "embeds": [embed]}
    try:
        r = requests.post(webhook_url, json=payload, timeout=15)
        if r.status_code >= 300:
            print(f"{YELLOW}Discord webhook returned {r.status_code}: {r.text[:200]}{RESET}")
    except Exception as e:
        print(f"{YELLOW}Failed to send Discord notification: {e}{RESET}")

# ========================
# Worker thread per monitor
# ========================

# MonitorWorker with hard-sticky state and stable error signatures
# MonitorWorker with stable, coarse error kinds (no more spam on getaddrinfo flips)
class MonitorWorker(threading.Thread):
    def __init__(self, monitor: Monitor, state: AppState, alert_queue: "queue.Queue[str]", stop_evt: threading.Event):
        super().__init__(daemon=True)
        self.monitor = monitor
        self.state = state
        self.alert_queue = alert_queue
        self.stop_evt = stop_evt

    # ---------- helpers for sticky error signatures ----------

    def _host_from_url(self, url: str) -> str:
        try:
            if "://" in url:
                return url.split("://", 1)[1].split("/", 1)[0]
            return url.split("/", 1)[0]
        except Exception:
            return ""

    def _root_exc(self, e: BaseException) -> BaseException:
        cur = e
        seen = set()
        while True:
            nxt = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
            if not nxt or nxt in seen:
                return cur
            seen.add(nxt)
            cur = nxt

    def _errno_of(self, e: BaseException) -> Optional[int]:
        for attr in ("errno", "winerror"):
            v = getattr(e, attr, None)
            if isinstance(v, int):
                return v
        try:
            if e.args and isinstance(e.args[0], int):
                return e.args[0]
        except Exception:
            pass
        return None

    # classify net errors to a coarse, stable kind
    def _classify_net_error(self, e: BaseException) -> str:
        import socket
        try:
            import urllib3
            urexc = getattr(urllib3, "exceptions", None)
        except Exception:
            urexc = None

        root = self._root_exc(e)
        name = type(root).__name__.lower()

        # DNS / name resolution
        if "gaierror" in name or "nameresolutionerror" in name:
            return "dns"
        if urexc and isinstance(root, getattr(urexc, "NameResolutionError", tuple())):
            return "dns"

        # Timeouts (requests/urllib3/socket)
        if "timeout" in name or "readtimeout" in name or "connecttimeout" in name:
            return "timeout"

        # TLS/SSL errors
        if "ssl" in name or "tls" in name or "ssLError".lower() in name:
            return "tls"

        # Proxy / blocked
        if "proxy" in name or "blocked" in name:
            return "proxy"

        # Connection problems (refused/reset)
        if "connectionerror" in name or "newconnectionerror" in name or "connectionreset" in name or "refused" in name:
            return "connect"

        # Fallback
        return "other"

    # build a stable signature using coarse kind + host only
    def _stable_net_error_signature(self, e: BaseException, url: str) -> str:
        kind = self._classify_net_error(e)
        host = self._host_from_url(url)
        return f"{kind}:{host}"

    def _status_tuple(self, t: Optional[str], code: Optional[str], sig: Optional[str]) -> tuple:
        return (t or "", code or "", sig or "")

    def _status_changed(self, new_t: Optional[str], new_code: Optional[str], new_sig: Optional[str]) -> bool:
        return self._status_tuple(new_t, new_code, new_sig) != \
               self._status_tuple(self.monitor.last_state, self.monitor.last_code, self.monitor.last_sig)

    def _update_status(self, new_t: Optional[str], new_code: Optional[str], new_sig: Optional[str]) -> None:
        self.monitor.last_state = new_t
        self.monitor.last_code  = new_code
        self.monitor.last_sig   = new_sig

    def _selector_label(self) -> str:
        return self.monitor.selector if self.monitor.selector else "(whole page)"

    # ---------- main loop ----------

    def run(self):
        first_run = True
        while not self.stop_evt.is_set():
            start_ts = time.time()
            try:
                # fetch
                if self.monitor.use_js:
                    content = fetch_rendered_content(
                        self.monitor.url,
                        self.monitor.selector,
                        self.monitor.mode,
                        wait_ms=max(0, int(self.monitor.js_wait_ms)),
                        polls=3,
                        poll_interval_ms=400,
                    )
                    new_etag = None
                    new_lm = None
                    status_ok = True
                    status_code = 200
                else:
                    resp = fetch_url(self.monitor.url, self.monitor.etag, self.monitor.last_modified, self.monitor.headers)
                    status_code = resp.status_code
                    if resp.status_code == 304:
                        status_ok = True
                        content = None
                        new_etag = self.monitor.etag
                        new_lm = self.monitor.last_modified
                    elif 200 <= resp.status_code < 300:
                        status_ok = True
                        new_etag = resp.headers.get("ETag")
                        new_lm = resp.headers.get("Last-Modified")
                        content = extract_content(resp.text, self.monitor.selector, self.monitor.mode)
                    else:
                        status_ok = False
                        content = None
                        new_etag = None
                        new_lm = None

                # http error path
                if not status_ok:
                    ts_footer = time.strftime("%Y-%m-%d %H:%M:%S")
                    new_t, new_code, new_sig = "http_error", str(status_code), None
                    if self._status_changed(new_t, new_code, new_sig):
                        msg = f"[WARN] {self.monitor.url} responded {status_code}"
                        self.alert_queue.put(f"{YELLOW}{msg}{RESET}")
                        send_discord(self.state.webhook_url, "Website Access Error",
                                     self.monitor.url, self.monitor.selector, self.monitor.mode,
                                     msg, footer_text=f"Detected at {ts_footer}")
                    elif self.state.verbose_status:
                        ts = time.strftime("%H:%M:%S")
                        self.alert_queue.put(f"{CYAN}[{ts}] {self.monitor.url} — still {status_code}.{RESET}")
                    self._update_status(new_t, new_code, new_sig)
                    save_monitor_delta(self.monitor)

                # ok path
                else:
                    if self.monitor.use_js and content is None:
                        if self.state.verbose_status:
                            ts = time.strftime("%H:%M:%S")
                            self.alert_queue.put(f"{CYAN}[{ts}] {self.monitor.url} — unstable DOM; change ignored.{RESET}")
                        self._update_status("ok", "200", None)
                        save_monitor_delta(self.monitor)
                    else:
                        # one-time recovery alert
                        if (
                            self.monitor.last_state in ("http_error", "net_error")
                            and self.monitor.last_hash is not None
                            and self._status_changed("ok", "200", None)
                        ):
                            ts_footer = time.strftime("%Y-%m-%d %H:%M:%S")
                            rec_msg = f"[RECOVERED] {self.monitor.url} is reachable."
                            self.alert_queue.put(f"{GREEN}{rec_msg}{RESET}")
                            send_discord(self.state.webhook_url, "Website Recovered",
                                         self.monitor.url, self.monitor.selector, self.monitor.mode,
                                         rec_msg, footer_text=f"Detected at {ts_footer}")

                        self._update_status("ok", "200", None)

                        # no-change (304)
                        if content is None:
                            if self.state.verbose_status:
                                ts = time.strftime("%H:%M:%S")
                                self.alert_queue.put(f"{CYAN}[{ts}] {self.monitor.url} — no change (304).{RESET}")
                            save_monitor_delta(self.monitor)
                        else:
                            content_str = content or ""
                            is_empty_now = (content_str.strip() == "")

                            # content-only guard (unchanged)
                            if self.monitor.content_only_changes:
                                if is_empty_now:
                                    self.monitor.was_empty = True
                                    save_monitor_delta(self.monitor)
                                    first_run = False
                                else:
                                    if self.monitor.was_empty:
                                        old_excerpt = self.monitor.last_excerpt or ""
                                        d = diff_excerpt(old_excerpt, content_str)
                                        ts_hms = time.strftime("%H:%M:%S")
                                        ts_footer = time.strftime("%Y-%m-%d %H:%M:%S")

                                        self.monitor.last_hash = compute_hash(content_str)
                                        self.monitor.last_excerpt = content_str[:2000]
                                        self.monitor.etag = new_etag
                                        self.monitor.last_modified = new_lm
                                        self.monitor.was_empty = False
                                        save_monitor_delta(self.monitor)

                                        combined = (
                                            f"{RED}[CHANGE {ts_hms}] {self.monitor.url} — {self._selector_label()} changed! {RESET}"
                                            f"{CYAN}{self.monitor.url}{RESET}"
                                            f"{colorize_diff(d)}"
                                        )
                                        self.alert_queue.put(combined)
                                        send_discord(self.state.webhook_url, "Website Change Detected",
                                                     self.monitor.url, self.monitor.selector, self.monitor.mode,
                                                     d, footer_text=f"Detected at {ts_footer}")

                            new_hash = compute_hash(content_str)

                            if first_run and not self.monitor.last_hash:
                                self.monitor.last_hash = new_hash
                                self.monitor.last_excerpt = content_str[:2000]
                                self.monitor.etag = new_etag
                                self.monitor.last_modified = new_lm
                                if self.monitor.content_only_changes:
                                    self.monitor.was_empty = is_empty_now
                                save_monitor_delta(self.monitor)
                                if self.state.verbose_status:
                                    self.alert_queue.put(f"{GREEN}[BASELINE] {self.monitor.url} — {self._selector_label()} captured baseline.{RESET}")
                            elif new_hash != self.monitor.last_hash:
                                time.sleep(1.0)
                                content2 = None
                                try:
                                    if self.monitor.use_js:
                                        content2 = fetch_rendered_content(
                                            self.monitor.url,
                                            self.monitor.selector,
                                            self.monitor.mode,
                                            wait_ms=max(0, int(self.monitor.js_wait_ms)),
                                            polls=2,
                                            poll_interval_ms=350,
                                        )
                                    else:
                                        resp2 = requests.get(self.monitor.url, headers=self.monitor.headers, timeout=30)
                                        if 200 <= resp2.status_code < 300:
                                            content2 = extract_content(resp2.text, self.monitor.selector, self.monitor.mode)
                                except Exception:
                                    content2 = None

                                if content2 is not None and compute_hash(content2) == new_hash:
                                    if self.monitor.content_only_changes and (content2.strip() == ""):
                                        self.monitor.was_empty = True
                                        save_monitor_delta(self.monitor)
                                    else:
                                        old_excerpt = self.monitor.last_excerpt or ""
                                        d = diff_excerpt(old_excerpt, content_str)
                                        ts_hms = time.strftime("%H:%M:%S")
                                        ts_footer = time.strftime("%Y-%m-%d %H:%M:%S")

                                        self.monitor.last_hash = new_hash
                                        self.monitor.last_excerpt = content_str[:2000]
                                        self.monitor.etag = new_etag
                                        self.monitor.last_modified = new_lm
                                        if self.monitor.content_only_changes:
                                            self.monitor.was_empty = False
                                        save_monitor_delta(self.monitor)

                                        combined = (
                                            f"{RED}[CHANGE {ts_hms}] {self.monitor.url} — {self._selector_label()} changed! {RESET}"
                                            f"{CYAN}{self.monitor.url}{RESET}"
                                            f"{colorize_diff(d)}"
                                        )
                                        self.alert_queue.put(combined)
                                        send_discord(self.state.webhook_url, "Website Change Detected",
                                                     self.monitor.url, self.monitor.selector, self.monitor.mode,
                                                     d, footer_text=f"Detected at {ts_footer}")
                                else:
                                    if self.state.verbose_status:
                                        ts = time.strftime("%H:%M:%S")
                                        self.alert_queue.put(f"{CYAN}[{ts}] {self.monitor.url} — transient change ignored.{RESET}")
                                    save_monitor_delta(self.monitor)
                            else:
                                if self.state.verbose_status:
                                    ts = time.strftime("%H:%M:%S")
                                    self.alert_queue.put(f"{CYAN}[{ts}] {self.monitor.url} — checked, no change.{RESET}")
                                save_monitor_delta(self.monitor)

            except Exception as e:
                ts_footer = time.strftime("%Y-%m-%d %H:%M:%S")
                # coarse, stable kind + signature => no repeated alerts for 11001/11002 flips
                kind = self._classify_net_error(e)
                sig = self._stable_net_error_signature(e, self.monitor.url)
                new_t, new_code, new_sig = "net_error", kind, sig

                if self._status_changed(new_t, new_code, new_sig):
                    msg = f"[ERROR] {self.monitor.url}: {e}"
                    self.alert_queue.put(f"{YELLOW}{msg}{RESET}")
                    send_discord(self.state.webhook_url, "Website Access Error",
                                 self.monitor.url, self.monitor.selector, self.monitor.mode,
                                 msg, footer_text=f"Detected at {ts_footer}")
                elif self.state.verbose_status:
                    ts = time.strftime("%H:%M:%S")
                    self.alert_queue.put(f"{CYAN}[{ts}] {self.monitor.url} — still unreachable ({kind}).{RESET}")

                self._update_status(new_t, new_code, new_sig)
                save_monitor_delta(self.monitor)

            first_run = False
            elapsed = time.time() - start_ts
            remaining = max(1, self.monitor.interval_sec - int(elapsed))
            for _ in range(remaining):
                if self.stop_evt.is_set():
                    break
                time.sleep(1)


# ========================
# UI helpers
# ========================

def clear():
    try:
        os.system("cls" if os.name == "nt" else "clear")
    except Exception:
        pass

def _lerp(a, b, t):
    return int(a + (b - a) * t)

def make_multi_gradient(stops, steps):
    if steps <= 1:
        return [stops[0]]
    segs = len(stops) - 1
    out = []
    for i in range(steps):
        pos = i * segs / (steps - 1)
        idx = int(pos)
        if idx >= segs:
            idx = segs - 1
            t = 1.0
        else:
            t = pos - idx
        r = _lerp(stops[idx][0], stops[idx + 1][0], t)
        g = _lerp(stops[idx][1], stops[idx + 1][1], t)
        b = _lerp(stops[idx][2], stops[idx + 1][2], t)
        out.append((r, g, b))
    return out

def print_header() -> str:
    # Do not force terminal resize; respect user's window size
    banner = r"""
░▒▓██████████████▓▒░ ░▒▓██████▓▒░░▒▓███████▓▒░░▒▓██████████████▓▒░ ░▒▓██████▓▒░░▒▓███████▓▒░  
░▒▓█▓▒░░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░ 
░▒▓█▓▒░░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░ 
░▒▓█▓▒░░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░ 
░▒▓█▓▒░░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░ 
░▒▓█▓▒░░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░ 
░▒▓█▓▒░░▒▓█▓▒░░▒▓█▓▒░░▒▓██████▓▒░░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░░▒▓█▓▒░░▒▓██████▓▒░░▒▓█▓▒░░▒▓█▓▒░ 
                                                 Python Website Monitor w/ Discord Webhooks   
"""
    os.system("")
    faded_banner = ""
    lines = banner.splitlines()
    stops = [
        (85, 0, 145),     # deep purple
        (122, 87, 176),   # mid purple
        (199, 162, 255),  # lilac
    ]
    colors = make_multi_gradient(stops, max(1, len(lines)))
    for (line, (r, g, b)) in zip(lines, colors):
        faded_banner += (f"\033[38;2;{r};{g};{b}m{line}\033[0m\n")
    return faded_banner

def prompt_int(prompt: str, default: int, min_v: int = 1, max_v: int = 86400) -> int:
    while True:
        s = input(f"{prompt} [{default}]: ").strip()
        if not s:
            return default
        if s.isdigit():
            v = int(s)
            if min_v <= v <= max_v:
                return v
        print(f"{YELLOW}Enter a number between {min_v} and {max_v}.{RESET}")

def prompt_choice(prompt: str, choices: List[str], default: str) -> str:
    choices_lower = [c.lower() for c in choices]
    default_low = default.lower()
    while True:
        s = input(f"{prompt} {choices} [{default}]: ").strip().lower()
        if not s:
            return default
        if s in choices_lower:
            return choices[choices_lower.index(s)]
        print(f"{YELLOW}Choose one of: {choices}{RESET}")
        
    
# normalize CSS selector input (quotes -> stripped; "a b c" -> ".a.b.c")
def _normalize_selector_input(sel: Optional[str]) -> Optional[str]:
    # accepts None and returns None to be drop-in safe
    if not sel:
        return sel
    s = sel.strip()

    # strip wrapping quotes
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()

    # if it already looks like a real CSS selector, keep it
    # (has ., #, [], combinators, etc.)
    import re
    if re.search(r'[.#:\[\]>+~=*]', s):
        return s

    # allow comma-separated groups; convert each group of bare class tokens
    groups = [g for g in re.split(r'\s*,\s*', s) if g]
    out_groups = []
    for g in groups:
        tokens = [t for t in re.split(r'\s+', g.strip()) if t]
        if not tokens:
            continue
        # only convert if tokens look like class-name fragments (avoid mangling tags)
        if not all(re.match(r'^[A-Za-z_][-A-Za-z0-9_]*$', t) for t in tokens):
            return s  # bail out, user probably typed real CSS already
        out_groups.append('.' + '.'.join(tokens))
    return ','.join(out_groups) if out_groups else s

# one-shot preview (HTTP or JS) for selector checks
def preview_selection(url: str, selector: Optional[str], mode: str, use_js: bool, js_wait_ms: int, js_wait_selector: Optional[str]) -> str:
    try:
        # normalize wait_for so preview matches runtime behaviour
        norm_wait = _normalize_selector_input(js_wait_selector) if js_wait_selector else None
        if use_js:
            html = fetch_rendered_html(url, js_wait_ms, norm_wait)
            content = extract_content_from_html(html, selector, mode)
        else:
            resp = fetch_url(url, None, None, {})
            if not (200 <= resp.status_code < 300):
                return f"[ERROR] preview request returned {resp.status_code}"
            content = extract_content(resp.text, selector, mode)
        preview = (content or "")
        if len(preview) > 800:
            preview = preview[:800] + "\n... (truncated preview) ..."
        return preview
    except Exception as e:
        return f"[ERROR] {e}"

def add_monitor_flow(state: AppState) -> None:

    try:
        clear()
        stdout.write(print_header())
    except Exception:
        pass
    url = input("Enter URL to monitor: ").strip()
    if not url:
        print(f"{YELLOW}URL is required.{RESET}")
        return

    selector = input("CSS selector to focus (blank for whole page): ").strip()
    # normalize main selector input
    selector = _normalize_selector_input(selector) if selector else None

    mode = prompt_choice("Compare extracted 'text' or raw 'html'?", ["text", "html"], "text")
    interval = prompt_int("Check frequency in seconds", default=60, min_v=5)

    custom_header = {}
    ua = input("Custom User-Agent (blank to use default): ").strip()
    if ua:
        custom_header["User-Agent"] = ua

    use_js_ans = prompt_choice("Use JS-rendered mode (Playwright)?", ["y", "n"], "n")
    use_js = (use_js_ans.lower() == "y")
    js_wait_ms = 3000
    js_wait_selector = None
    if use_js:
        if not _playwright_available:
            print(f"{YELLOW}Playwright not detected. Install with:{RESET}")
            print(f"{YELLOW}  pip install playwright && python -m playwright install chromium{RESET}")
        js_wait_ms = prompt_int("Wait milliseconds after load (JS settle time)", default=3000, min_v=0, max_v=60000)
        tmp = input("Optional CSS selector to wait for (blank to skip): ").strip()
        # normalize wait_for input
        js_wait_selector = _normalize_selector_input(tmp) if tmp else None

    # offer content-only mode at creation time
    co_ans = prompt_choice("Enable content-only changes (ignore empty/missing content)?", ["y", "n"], "n")
    content_only = (co_ans.lower() == "y")

    # preview loop when a selector is provided
    if selector:
        while True:
            print(f"{CYAN}Previewing selector `{selector}` (mode={mode}, JS={use_js})...{RESET}")
            prev = preview_selection(url, selector, mode, use_js, js_wait_ms, js_wait_selector)
            if prev.startswith("[ERROR]"):
                print(f"{YELLOW}{prev}{RESET}")
                ans = prompt_choice("Continue anyway?", ["y", "n"], "n")
                if ans.lower() == "y":
                    break
            else:
                if not prev.strip():
                    print(f"{YELLOW}Selector matched NO content. You probably want to re-enter the selector.{RESET}")
                else:
                    print(f"{GREEN}--- Selector Preview Start ---{RESET}\n{prev}\n{GREEN}--- Selector Preview End ---{RESET}")
                ans = prompt_choice("Accept this selector?", ["y", "n"], "y" if prev.strip() else "n")
                if ans.lower() == "y":
                    break
                # allow user to re-enter, then normalize again
                new_in = input("Enter new CSS selector (blank to cancel): ").strip()
                if not new_in:
                    print(f"{YELLOW}Cancelled adding monitor.{RESET}")
                    return
                selector = _normalize_selector_input(new_in)

    m = Monitor(
        id=str(uuid.uuid4()),
        url=url,
        selector=selector,
        mode=mode,
        interval_sec=interval,
        headers=custom_header,
        last_hash=None,
        last_excerpt=None,
        etag=None,
        last_modified=None,
        use_js=use_js,
        js_wait_ms=js_wait_ms,
        js_wait_selector=js_wait_selector,
        content_only_changes=content_only,   # 
        was_empty=None                       # (baseline handled on first run)
    )
    state.monitors.append(m)
    save_state(state)
    print(f"{GREEN}Added monitor for {url} ({selector or 'whole page'}), every {interval}s.{RESET}")
    if use_js:
        print(f"{CYAN}JS-rendered mode enabled (wait {js_wait_ms} ms; wait_for: {js_wait_selector or 'None'}).{RESET}")
    if content_only:
        print(f"{CYAN}Content-only changes: ON — empty/missing content will not trigger alerts until content appears.{RESET}")

def remove_monitor_flow(state: AppState) -> None:
    reload_state_into(state)

    if not state.monitors:
        try:
            clear(); stdout.write(print_header())
        except Exception:
            pass
        print(f"{YELLOW}No monitors to remove.{RESET}")
        return

    list_monitors(state)
    idx = input("Enter index to remove: ").strip()
    try:
        i = int(idx)
        if 0 <= i < len(state.monitors):
            removed = state.monitors.pop(i)
            # mark as tombstoned
            if state.deleted_ids is None:
                state.deleted_ids = []
            if removed.id not in state.deleted_ids:
                state.deleted_ids.append(removed.id)
            save_state(state)
            reload_state_into(state)
            print(f"{GREEN}Removed {removed.url} ({removed.selector or 'whole page'}).{RESET}")
        else:
            print(f"{YELLOW}Index out of range.{RESET}")
    except Exception:
        print(f"{YELLOW}Invalid index.{RESET}")



def edit_monitor_flow(state: AppState, index: Optional[int] = None) -> None:
    reload_state_into(state)  # sync at entry

    if not state.monitors:
        try:
            clear(); stdout.write(print_header())
        except Exception:
            pass
        print(f"{YELLOW}No monitors to edit.{RESET}")
        return

    if index is None:
        list_monitors(state)
        idx = input("Enter index to edit: ").strip()
        try:
            i = int(idx)
            if not (0 <= i < len(state.monitors)):
                print(f"{YELLOW}Index out of range.{RESET}")
                return
        except Exception:
            print(f"{YELLOW}Invalid index.{RESET}")
            return
    else:
        i = index
    m = state.monitors[i]

    if _rich_available:
        width = _ui_width()
        while True:
            try:
                clear(); stdout.write(print_header())
            except Exception:
                pass

            summary = Table(box=box.SIMPLE, show_edge=False, expand=False, padding=(0,1))
            summary.width = width - 2
            summary.add_column("Field", style="cyan", no_wrap=True)
            summary.add_column("Value", overflow="fold", max_width=width-16)
            summary.add_row("1 URL", m.url)
            summary.add_row("2 Selector", m.selector or "(whole page)")
            summary.add_row("3 Mode", m.mode)
            summary.add_row("4 Every", f"{m.interval_sec}s")
            summary.add_row("5 UA", (m.headers or {}).get("User-Agent", "(default)"))
            summary.add_row("6 JS", "on" if m.use_js else "off")
            summary.add_row("7 JS wait", f"{m.js_wait_ms} ms" if m.use_js else "-")
            summary.add_row("8 wait_for", m.js_wait_selector or "(none)")
            summary.add_row("9 Content-only changes", "on" if m.content_only_changes else "off")  # keep visible

            menu = Table.grid(padding=0)
            menu.add_column()
            menu.add_row("Enter 1-9 to edit a field   S) Save   R) Reset   P) Preview   Q) Cancel")
            _console.print(Panel(summary, title=f"Edit Monitor #{i}", border_style="purple", width=width))
            _console.print(Panel(menu, border_style="purple", width=width))
            cmd = input("Choice: ").strip().lower()
            if cmd == "1":
                new_url = input("New URL (blank to keep): ").strip()
                if new_url:
                    m.url = new_url
            elif cmd == "2":
                new_sel = input("New CSS selector (blank for whole page): ").strip()
                m.selector = _normalize_selector_input(new_sel) if new_sel else None
            elif cmd == "3":
                m.mode = prompt_choice("Mode", ["text", "html"], m.mode)
            elif cmd == "4":
                try:
                    s = input(f"Every seconds [{m.interval_sec}]: ").strip()
                    if s and s.isdigit() and int(s) >= 1:
                        m.interval_sec = int(s)
                except Exception:
                    pass
            elif cmd == "5":
                ua_in = input("Custom User-Agent (blank to clear/use default): ").strip()
                if ua_in:
                    m.headers = dict(m.headers or {})
                    m.headers["User-Agent"] = ua_in
                else:
                    if m.headers and "User-Agent" in m.headers:
                        m.headers.pop("User-Agent", None)
                        if not m.headers:
                            m.headers = {}
            elif cmd == "6":
                m.use_js = not m.use_js
                if not m.use_js:
                    m.js_wait_ms = 3000
                    m.js_wait_selector = None
            elif cmd == "7":
                if m.use_js:
                    m.js_wait_ms = prompt_int("JS wait ms", default=m.js_wait_ms, min_v=0, max_v=60000)
            elif cmd == "8":
                if m.use_js:
                    tmp = input("wait_for selector (blank for none): ").strip()
                    m.js_wait_selector = _normalize_selector_input(tmp) if tmp else None
            elif cmd == "9":
                m.content_only_changes = not m.content_only_changes
                if m.content_only_changes:
                    print(f"{GREEN}Content-only changes: ON (empty content is ignored).{RESET}")
                else:
                    print(f"{GREEN}Content-only changes: OFF.{RESET}")
                time.sleep(0.6)
            elif cmd == "p":
                print(f"{CYAN}Previewing `{m.selector or '(whole page)'}` (mode={m.mode}, JS={m.use_js})...{RESET}")
                prev = preview_selection(m.url, m.selector, m.mode, m.use_js, m.js_wait_ms, m.js_wait_selector)
                if prev.startswith("[ERROR]"):
                    print(f"{YELLOW}{prev}{RESET}")
                else:
                    if not prev.strip():
                        print(f"{YELLOW}Selector matched NO content.{RESET}")
                    else:
                        print(f"{GREEN}--- Selector Preview Start ---{RESET}\n{prev}\n{GREEN}--- Selector Preview End ---{RESET}")
                input("Press Enter to continue...")
            elif cmd == "r":
                m.last_hash = None; m.last_excerpt = None; m.etag = None; m.last_modified = None
                m.was_empty = None  # 
                print(f"{GREEN}Baseline reset. It will re-baseline on next check.{RESET}")
                time.sleep(0.6)
            elif cmd == "s":
                # per-monitor delta save (prevents stale full-list rewrites)
                save_monitor_delta(m)
                reload_state_into(state)
                print(f"{GREEN}Monitor updated.{RESET}")
                time.sleep(0.6)
                return
            elif cmd == "q":
                print("Cancelled.")
                time.sleep(0.4)
                return
            else:
                print(f"{YELLOW}Unknown choice.{RESET}")
                time.sleep(0.6)
    else:
        # text fallback
        print(f"Editing monitor [{i}] — leave blank to keep current value")
        print(f"Current URL: {m.url}")
        new_url = input("New URL: ").strip()
        if new_url:
            m.url = new_url
        print(f"Current selector: {m.selector or '(whole page)'}")
        new_sel = input("New CSS selector (blank for whole page): ").strip()
        if new_sel or new_sel == "":
            m.selector = _normalize_selector_input(new_sel) if new_sel else None
        m.mode = prompt_choice("Compare 'text' or 'html'?", ["text", "html"], m.mode)
        try:
            new_interval = input(f"Check frequency in seconds [{m.interval_sec}]: ").strip()
            if new_interval and new_interval.isdigit() and int(new_interval) >= 1:
                m.interval_sec = int(new_interval)
        except Exception:
            pass
        current_ua = m.headers.get("User-Agent", "") if m.headers else ""
        print(f"Current custom User-Agent: {current_ua or '(none)'}")
        ua_in = input("New custom User-Agent (blank to clear/keep none): ").strip()
        if ua_in or ua_in == "":
            if ua_in:
                m.headers = dict(m.headers or {})
                m.headers["User-Agent"] = ua_in
            else:
                if m.headers and "User-Agent" in m.headers:
                    m.headers.pop("User-Agent", None)
                    if not m.headers:
                        m.headers = {}
        m.use_js = prompt_choice("Use JS-rendered mode?", ["y", "n"], "y" if m.use_js else "n").lower() == "y"
        if m.use_js:
            m.js_wait_ms = prompt_int("Wait milliseconds after load (JS settle time)", default=m.js_wait_ms, min_v=0, max_v=60000)
            print(f"Current wait_for selector: {m.js_wait_selector or '(none)'}")
            tmp = input("New wait_for selector (blank for none): ").strip()
            m.js_wait_selector = _normalize_selector_input(tmp) if tmp else None
        else:
            m.js_wait_ms = 3000
            m.js_wait_selector = None

        co = prompt_choice("Enable content-only changes (ignore empty)?", ["y", "n"], "n" if not m.content_only_changes else "y")
        m.content_only_changes = (co.lower() == "y")

        reset_baseline = prompt_choice("Reset baseline (recommended after edits)?", ["y", "n"], "y")
        if reset_baseline.lower() == "y":
            m.last_hash = None
            m.last_excerpt = None
            m.etag = None
            m.last_modified = None
            m.was_empty = None  # 
        # per-monitor delta save
        save_monitor_delta(m)
        reload_state_into(state)
        print(f"{GREEN}Monitor updated.{RESET}")


def list_monitors(state: AppState) -> None:

    try:
        clear()
        stdout.write(print_header())
    except Exception:
        pass
    if not state.monitors:
        print(f"{YELLOW}No monitors configured yet.{RESET}")
        return
    if _rich_available:
        width = _ui_width()
        table = Table(title="Configured Monitors", box=box.SIMPLE)
        table.width = width
        table.add_column("#", style="bold cyan", no_wrap=True)
        table.add_column("URL", overflow="fold", max_width=max(20, width-34))
        table.add_column("Sel", overflow="fold", max_width=12)
        table.add_column("Mode", style="magenta", no_wrap=True)
        table.add_column("Every", style="green", no_wrap=True)
        table.add_column("JS", style="yellow", no_wrap=True)
        for i, m in enumerate(state.monitors):
            table.add_row(
                str(i),
                m.url,
                (m.selector or "(page)")[:12],
                m.mode,
                f"{m.interval_sec}s",
                "yes" if m.use_js else "no",
            )
        _console.print(Panel(table, border_style="purple", width=width))
    else:
        for i, m in enumerate(state.monitors):
            print(f"[{i}] {m.url}  | selector: {m.selector or '(whole page)'} | mode: {m.mode} | every {m.interval_sec}s | use_js={m.use_js}")


def manage_monitors_flow(state: AppState) -> None:
    while True:
        reload_state_into(state)  # keep memory in sync each loop

        try:
            clear(); stdout.write(print_header())
        except Exception:
            pass

        if _rich_available:
            width = _ui_width()
            table = Table(title="Monitors", box=box.SIMPLE)
            table.width = width
            table.add_column("#", style="bold cyan", no_wrap=True)
            table.add_column("URL", overflow="fold", max_width=max(20, width-34))
            table.add_column("Sel", overflow="fold", max_width=12)
            table.add_column("Mode", style="magenta", no_wrap=True)
            table.add_column("Every", style="green", no_wrap=True)
            table.add_column("JS", style="yellow", no_wrap=True)
            for i, m in enumerate(state.monitors):
                table.add_row(
                    str(i),
                    m.url,
                    (m.selector or "(whole page)"),
                    m.mode,
                    f"{m.interval_sec}s",
                    "on" if m.use_js else "off",
                )
            _console.print(table)
            actions = Table.grid(padding=0)
            actions.add_column()
            actions.add_row("A) Add   E) Edit   R) Remove   V) Preview   Q) Back")
            _console.print(Panel(actions, border_style="purple", width=width))
        else:
            if not state.monitors:
                print(f"{YELLOW}No monitors configured yet.{RESET}")
            else:
                for i, m in enumerate(state.monitors):
                    print(f"[{i}] {m.url}  | selector: {m.selector or '(whole page)'} | mode: {m.mode} | every {m.interval_sec}s | use_js={m.use_js}")
            print("\nA) Add   E) Edit   R) Remove   V) Preview   Q) Back")

        cmd = input("Choice: ").strip().lower()
        if cmd == "a":
            add_monitor_flow(state)
        elif cmd == "e":
            if not state.monitors:
                print(f"{YELLOW}No monitors to edit.{RESET}"); time.sleep(0.7); continue
            s = input("Index to edit: ").strip()
            if not s.isdigit():
                print(f"{YELLOW}Enter a valid index.{RESET}"); time.sleep(0.7); continue
            i = int(s)
            if 0 <= i < len(state.monitors):
                edit_monitor_flow(state, i)
            else:
                print(f"{YELLOW}Index out of range.{RESET}"); time.sleep(0.7)
        elif cmd == "r":
            if not state.monitors:
                print(f"{YELLOW}No monitors to remove.{RESET}"); time.sleep(0.7); continue
            s = input("Index to remove: ").strip()
            try:
                i = int(s)
                if 0 <= i < len(state.monitors):
                    removed = state.monitors.pop(i)
                    # tombstone the id so no path can resurrect it
                    if state.deleted_ids is None:
                        state.deleted_ids = []
                    if removed.id not in state.deleted_ids:
                        state.deleted_ids.append(removed.id)
                    save_state(state)
                    reload_state_into(state)
                    print(f"{GREEN}Removed {removed.url} ({removed.selector or 'whole page'}).{RESET}")
                    time.sleep(0.7)
                else:
                    print(f"{YELLOW}Index out of range.{RESET}"); time.sleep(0.7)
            except Exception:
                print(f"{YELLOW}Invalid index.{RESET}"); time.sleep(0.7)
        elif cmd == "v":
            if not state.monitors:
                print(f"{YELLOW}No monitors to preview.{RESET}"); time.sleep(0.7); continue
            s = input("Index to preview: ").strip()
            if not s.isdigit():
                print(f"{YELLOW}Enter a valid index.{RESET}"); time.sleep(0.7); continue
            i = int(s)
            if not (0 <= i < len(state.monitors)):
                print(f"{YELLOW}Index out of range.{RESET}"); time.sleep(0.7); continue
            m = state.monitors[i]
            print(f"{CYAN}Previewing `{m.selector or '(whole page)'}` (mode={m.mode}, JS={m.use_js})...{RESET}")
            prev = preview_selection(m.url, m.selector, m.mode, m.use_js, m.js_wait_ms, m.js_wait_selector)
            if prev.startswith("[ERROR]"):
                print(f"{YELLOW}{prev}{RESET}")
            else:
                if not prev.strip():
                    print(f"{YELLOW}Selector matched NO content.{RESET}")
                else:
                    print(f"{GREEN}--- Selector Preview Start ---{RESET}\n{prev}\n{GREEN}--- Selector Preview End ---{RESET}")
            input("Press Enter to continue...")
        elif cmd == "q":
            return
        else:
            print(f"{YELLOW}Unknown choice.{RESET}"); time.sleep(0.7)

def set_webhook_flow(state: AppState) -> None:

    try:
        clear()
        stdout.write(print_header())
    except Exception:
        pass
    current = state.webhook_url or "(not set)"
    if _rich_available:
        width = _ui_width()
        table = Table(box=box.SIMPLE, show_edge=False)
        table.width = width - 2
        table.add_column("Field", style="cyan", no_wrap=True)
        table.add_column("Value")
        table.add_row("Current", current)
        instructions = "Paste your Discord webhook URL to update, or leave blank to keep the current value."
        _console.print(Panel(table, title="Discord Webhook", border_style="purple", width=width))
        _console.print(Panel(instructions, border_style="purple", width=width))
    else:
        print(f"{CYAN}Current Discord webhook: {current}{RESET}")
    prompt_text = "Enter Discord webhook URL (or blank to keep): "
    try:
        new_url = Prompt.ask(prompt_text) if _rich_available else input(prompt_text)
    except Exception:
        new_url = input(prompt_text)
    new_url = (new_url or "").strip()
    if new_url:
        state.webhook_url = new_url
        save_state(state)
        print(f"{GREEN}Webhook updated.{RESET}")

def toggle_verbose_flow(state: AppState) -> None:

    try:
        clear()
        stdout.write(print_header())
    except Exception:
        pass
    state.verbose_status = not state.verbose_status
    save_state(state)
    print(f"{GREEN}Verbose status is now {'ON' if state.verbose_status else 'OFF'}.{RESET}")

def choose_user_agent_flow(state: AppState) -> None:

    try:
        clear()
        stdout.write(print_header())
    except Exception:
        pass
    global DEFAULT_USER_AGENT
    keys = list(PRESET_USER_AGENTS.keys())
    if _rich_available:
        width = _ui_width()
        table = Table(title="Default User-Agent", box=box.SIMPLE_HEAVY)
        table.width = width
        table.add_column("#", style="bold cyan", no_wrap=True)
        table.add_column("Preset", overflow="fold")
        table.add_column("Active", style="green", no_wrap=True)
        current_value = state.default_user_agent or DEFAULT_USER_AGENT
        for idx, name in enumerate(keys):
            is_current = PRESET_USER_AGENTS[name] == current_value
            table.add_row(str(idx), name, "• current" if is_current else "")
        _console.print(Panel(table, border_style="purple", width=width))
        footer = "Enter a number to select a preset, or press Enter to cancel."
        _console.print(Panel(footer, border_style="purple", width=width))
        try:
            s = Prompt.ask("Choice (number)")
        except Exception:
            s = input("Choice (number): ")
    else:
        print(f"{CYAN}Select a default User-Agent (used unless a monitor overrides it):{RESET}")
        for idx, name in enumerate(keys):
            marker = " (current)" if PRESET_USER_AGENTS[name] == (state.default_user_agent or DEFAULT_USER_AGENT) else ""
            print(f"  {idx}) {name}{marker}")
        s = input("Choice (number), or leave blank to cancel: ")
    s = (s or "").strip()
    if not s:
        return
    if not s.isdigit() or not (0 <= int(s) < len(keys)):
        print(f"{YELLOW}Invalid selection.{RESET}")
        return
    choice_name = keys[int(s)]
    ua = PRESET_USER_AGENTS[choice_name]
    DEFAULT_USER_AGENT = ua
    state.default_user_agent = ua
    save_state(state)
    print(f"{GREEN}Default User-Agent set to: {choice_name}{RESET}")

def ensure_webhook_set(state: AppState) -> None:
    while not state.webhook_url:
        # Keep banner visible while prompting
        try:
            clear()
            stdout.write(print_header())
        except Exception:
            pass
        print(f"{YELLOW}No Discord webhook is configured for alerts.{RESET}")
        new_url = input("Paste your Discord webhook URL (or press Enter to skip): ").strip()
        if not new_url:
            break
        state.webhook_url = new_url
        save_state(state)
        print(f"{GREEN}Webhook saved to {APP_STATE_FILE}.{RESET}")

# ========================
# Monitoring orchestration
# ========================

def start_monitoring(state: AppState) -> None:
    if not state.monitors:
        print(f"{YELLOW}No monitors configured. Add at least one first.{RESET}")
        return

    ensure_webhook_set(state)

    stop_evt = threading.Event()
    alert_q: "queue.Queue[str]" = queue.Queue()
    workers = [MonitorWorker(m, state, alert_q, stop_evt) for m in state.monitors]

    for w in workers:
        w.start()

    try:
        stdout.write(print_header())
        print(f"{GREEN}Monitoring started. Press Ctrl+C to stop.{RESET}")
        if state.webhook_url:
            print(f"{CYAN}Discord webhook is set: alerts will also be sent to your channel.{RESET}")
        else:
            print(f"{YELLOW}Discord webhook not set; alerts will appear only in this window.{RESET}")
        print(f"{CYAN}Tip: For JS dashboards, enable JS-rendered mode on that monitor.{RESET}")
        if state.verbose_status:
            print(f"{CYAN}Verbose status heartbeat is ON. Toggle from the main menu if noisy.{RESET}")
        while True:
            try:
                msg = alert_q.get(timeout=1.0)
                print(msg)
            except queue.Empty:
                pass
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Stopping...{RESET}")
        stop_evt.set()
        for w in workers:
            w.join(timeout=5.0)
        print(f"{GREEN}Stopped.{RESET}")

# ========================
# Main menu (Rich-enhanced)
# ========================

def _render_main_menu_rich(state: AppState) -> None:
    # Assumes screen was cleared and banner printed
    if not _rich_available:
        return
    width = _ui_width()
    table = Table(box=box.SIMPLE, show_edge=False, expand=False, padding=(0,1))
    table.width = width - 2
    table.add_column("#", style="bold cyan", no_wrap=True)
    table.add_column("Main Menu", style="bold white")
    table.add_row("0", "Start monitoring")
    table.add_row("1", "Manage monitors (Add/Edit/Remove/List)")
    table.add_row("2", "Set Discord webhook")
    table.add_row("3", f"Toggle status heartbeat (currently {'ON' if state.verbose_status else 'OFF'})")
    table.add_row("4", "Choose default User-Agent (presets)")
    table.add_row("5", "Quit")
    _console.print(Panel(table, border_style="purple", width=width))

def main_menu():
    state = load_state()
    if not state.webhook_url:
        ensure_webhook_set(state)

    while True:
        clear()
        stdout.write(print_header())
        if _rich_available:
            _render_main_menu_rich(state)
        else:
            print("0) Start monitoring")
            print("1) Manage monitors (Add/Edit/Remove/List)")
            print("2) Set Discord webhook")
            print(f"3) Toggle status heartbeat (currently {'ON' if state.verbose_status else 'OFF'})")
            print("4) Choose default User-Agent (presets)")
            print("5) Quit")
        choice = input("\nSelect: ").strip()
        clear()
        if choice == "0":
            start_monitoring(state)
        elif choice == "1":
            manage_monitors_flow(state)
        elif choice == "2":
            set_webhook_flow(state)
            input("Press Enter to continue...")
        elif choice == "3":
            toggle_verbose_flow(state)
        elif choice == "4":
            choose_user_agent_flow(state)
        elif choice == "5":
            print("Bye.")
            return
        else:
            print(f"{YELLOW}Invalid option.{RESET}")
            time.sleep(1)

if __name__ == "__main__":
    try:
        main_menu()
    except Exception as e:
        print(f"{YELLOW}Fatal error: {e}{RESET}")
        sys.exit(1)
