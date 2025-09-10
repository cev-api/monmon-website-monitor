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

APP_STATE_FILE = "monitor_state.json"
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

@dataclass
class AppState:
    webhook_url: Optional[str]
    monitors: List[Monitor]
    verbose_status: bool = False
    default_user_agent: Optional[str] = None

# ========================
# Persistence
# ========================

def load_state() -> AppState:
    global DEFAULT_USER_AGENT
    if not os.path.exists(APP_STATE_FILE):
        return AppState(webhook_url=None, monitors=[])
    try:
        with open(APP_STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        monitors = []
        for m in raw.get("monitors", []):
            monitors.append(Monitor(
                id=m.get("id") or str(uuid.uuid4()),
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
            ))
        webhook = raw.get("webhook_url")
        verbose_status = bool(raw.get("verbose_status", False))
        default_ua = raw.get("default_user_agent")
        if default_ua:
            DEFAULT_USER_AGENT = default_ua
        return AppState(
            webhook_url=webloghook if (webloghook := webhook) else None,
            monitors=monitors,
            verbose_status=verbose_status,
            default_user_agent=default_ua
        )
    except Exception as e:
        print(f"{YELLOW}Warning: failed to read state: {e}. Starting fresh.{RESET}")
        return AppState(webhook_url=None, monitors=[])

# robust Windows-friendly atomic save with retries
def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    attempts = 6
    delay = 0.2
    last_err = None
    for i in range(attempts):
        tmp = f"{path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last_err = e
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            time.sleep(delay)
            delay *= 1.5
        except Exception as e:
            last_err = e
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            time.sleep(delay)
    raise RuntimeError(f"Failed to save state after retries: {last_err}")

def save_state(state: AppState) -> None:
    data = {
        "webhook_url": state.webhook_url,
        "verbose_status": state.verbose_status,
        "default_user_agent": state.default_user_agent,
        "monitors": [asdict(m) for m in state.monitors],
    }
    _atomic_write_json(APP_STATE_FILE, data)

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
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(user_agent=DEFAULT_USER_AGENT)
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=max(1, wait_ms))
                except Exception:
                    pass
            else:
                time.sleep(wait_ms / 1000.0)
            html = page.content()
        finally:
            browser.close()
    return html

def extract_content_from_html(html_text: str, selector: Optional[str], mode: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    if selector:
        nodes = soup.select(selector)
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

class MonitorWorker(threading.Thread):
    def __init__(self, monitor: Monitor, state: AppState, alert_queue: "queue.Queue[str]", stop_evt: threading.Event):
        super().__init__(daemon=True)
        self.monitor = monitor
        self.state = state
        self.alert_queue = alert_queue
        self.stop_evt = stop_evt

    def run(self):
        first_run = True
        while not self.stop_evt.is_set():
            start_ts = time.time()
            try:
                if self.monitor.use_js:
                    html = fetch_rendered_html(
                        self.monitor.url,
                        wait_ms=max(0, int(self.monitor.js_wait_ms)),
                        wait_selector=self.monitor.js_wait_selector
                    )
                    content = extract_content_from_html(html, self.monitor.selector, self.monitor.mode)
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

                if not status_ok:
                    ts_footer = time.strftime("%Y-%m-%d %H:%M:%S")
                    msg = f"[WARN] {self.monitor.url} responded {status_code}"
                    self.alert_queue.put(f"{YELLOW}{msg}{RESET}")
                    send_discord(self.state.webhook_url, "Website Access Error", self.monitor.url, self.monitor.selector, self.monitor.mode, msg, footer_text=f"Detected at {ts_footer}")
                else:
                    if content is None:
                        if self.state.verbose_status:
                            ts = time.strftime("%H:%M:%S")
                            self.alert_queue.put(f"{CYAN}[{ts}] {self.monitor.url} — no change (304).{RESET}")
                    else:
                        if self.monitor.selector and not content.strip():
                            ts = time.strftime("%H:%M:%S")
                            self.alert_queue.put(f"{YELLOW}[{ts}] WARNING: selector returned no content for {self.monitor.url} — `{self._selector_label()}`{RESET}")
                        new_hash = compute_hash(content)
                        if first_run and not self.monitor.last_hash:
                            self.monitor.last_hash = new_hash
                            self.monitor.last_excerpt = content[:2000]
                            self.monitor.etag = new_etag
                            self.monitor.last_modified = new_lm
                            save_state(self.state)
                            self.alert_queue.put(f"{GREEN}[BASELINE] {self.monitor.url} — {self._selector_label()} captured baseline.{RESET}")
                        elif new_hash != self.monitor.last_hash:
                            old_excerpt = self.monitor.last_excerpt or ""
                            d = diff_excerpt(old_excerpt, content)

                            ts_hms = time.strftime("%H:%M:%S")
                            ts_footer = time.strftime("%Y-%m-%d %H:%M:%S")

                            # Update state
                            self.monitor.last_hash = new_hash
                            self.monitor.last_excerpt = content[:2000]
                            self.monitor.etag = new_etag
                            self.monitor.last_modified = new_lm
                            save_state(self.state)

                            # ### MODIFIED ### emit URL + diff as ONE clean block (no stray blank line)
                            combined = (
                                f"{RED}[CHANGE {ts_hms}] {self.monitor.url} — {self._selector_label()} changed! {RESET}\n"
                                f"{CYAN}{self.monitor.url}{RESET}\n"
                                f"{colorize_diff(d)}\n"  # ensure trailing newline for clean end
                            )
                            self.alert_queue.put(combined)

                            # Discord
                            send_discord(
                                self.state.webhook_url,
                                title="Website Change Detected",
                                url=self.monitor.url,
                                selector=self.monitor.selector,
                                mode=self.monitor.mode,
                                diff_text=d,
                                footer_text=f"Detected at {ts_footer}"
                            )
                        else:
                            if self.state.verbose_status:
                                ts = time.strftime("%H:%M:%S")
                                self.alert_queue.put(f"{CYAN}[{ts}] {self.monitor.url} — checked, no change.{RESET}")

            except Exception as e:
                ts_footer = time.strftime("%Y-%m-%d %H:%M:%S")
                msg = f"[ERROR] {self.monitor.url}: {e}"
                self.alert_queue.put(f"{YELLOW}{msg}{RESET}")
                send_discord(self.state.webhook_url, "Website Access Error", self.monitor.url, self.monitor.selector, self.monitor.mode, msg, footer_text=f"Detected at {ts_footer}")

            first_run = False
            elapsed = time.time() - start_ts
            remaining = max(1, self.monitor.interval_sec - int(elapsed))
            for _ in range(remaining):
                if self.stop_evt.is_set():
                    break
                time.sleep(1)

    def _selector_label(self) -> str:
        return self.monitor.selector if self.monitor.selector else "(whole page)"

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

# one-shot preview (HTTP or JS) for selector checks
def preview_selection(url: str, selector: Optional[str], mode: str, use_js: bool, js_wait_ms: int, js_wait_selector: Optional[str]) -> str:
    try:
        if use_js:
            html = fetch_rendered_html(url, js_wait_ms, js_wait_selector)
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
    if not selector:
        selector = None

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
        js_wait_selector = tmp if tmp else None

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
                selector = input("Enter new CSS selector (blank to cancel): ").strip()
                if not selector:
                    print(f"{YELLOW}Cancelled adding monitor.{RESET}")
                    return

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
        js_wait_selector=js_wait_selector
    )
    state.monitors.append(m)
    save_state(state)
    print(f"{GREEN}Added monitor for {url} ({selector or 'whole page'}), every {interval}s.{RESET}")
    if use_js:
        print(f"{CYAN}JS-rendered mode enabled (wait {js_wait_ms} ms; wait_for: {js_wait_selector or 'None'}).{RESET}")

def remove_monitor_flow(state: AppState) -> None:
    # Header will be printed by list_monitors below; but ensure banner if empty
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
            save_state(state)
            print(f"{GREEN}Removed {removed.url} ({removed.selector or 'whole page'}).{RESET}")
        else:
            print(f"{YELLOW}Index out of range.{RESET}")
    except Exception:
        print(f"{YELLOW}Invalid index.{RESET}")

def edit_monitor_flow(state: AppState, index: Optional[int] = None) -> None:
    # Streamlined Rich-driven edit menu (with text fallback)
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

            # Summary table with numeric field selectors
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

            menu = Table.grid(padding=0)
            menu.add_column()
            menu.add_row("Enter 1-8 to edit a field   S) Save   R) Reset   P) Preview   Q) Cancel")
            _console.print(Panel(summary, title=f"Edit Monitor #{i}", border_style="purple", width=width))
            _console.print(Panel(menu, border_style="purple", width=width))
            cmd = input("Choice: ").strip().lower()
            if cmd == "1":
                new_url = input("New URL (blank to keep): ").strip()
                if new_url:
                    m.url = new_url
            elif cmd == "2":
                new_sel = input("New CSS selector (blank for whole page): ").strip()
                m.selector = new_sel if new_sel else None
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
                    m.js_wait_selector = tmp if tmp else None
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
                print(f"{GREEN}Baseline reset. It will re-baseline on next check.{RESET}")
                time.sleep(0.6)
            elif cmd == "s":
                save_state(state)
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
        # Text fallback (simple prompts)
        print(f"Editing monitor [{i}] — leave blank to keep current value")
        print(f"Current URL: {m.url}")
        new_url = input("New URL: ").strip()
        if new_url:
            m.url = new_url
        print(f"Current selector: {m.selector or '(whole page)'}")
        new_sel = input("New CSS selector (blank for whole page): ").strip()
        if new_sel or new_sel == "":
            m.selector = new_sel if new_sel else None
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
            m.js_wait_selector = tmp if tmp else None
        else:
            m.js_wait_ms = 3000
            m.js_wait_selector = None
        reset_baseline = prompt_choice("Reset baseline (recommended after edits)?", ["y", "n"], "y")
        if reset_baseline.lower() == "y":
            m.last_hash = None
            m.last_excerpt = None
            m.etag = None
            m.last_modified = None
        save_state(state)
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
    # Integrated Add/Edit/Remove/List view
    while True:
        try:
            clear(); stdout.write(print_header())
        except Exception:
            pass

        # Render list inline
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
                    str(i), m.url, (m.selector or "(page)")[:12], m.mode, f"{m.interval_sec}s", "yes" if m.use_js else "no"
                )
            _console.print(Panel(table, border_style="purple", width=width))
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
                print(f"{YELLOW}No monitors to edit.{RESET}")
                time.sleep(0.7)
                continue
            s = input("Index to edit: ").strip()
            if not s.isdigit():
                print(f"{YELLOW}Enter a valid index.{RESET}")
                time.sleep(0.7)
                continue
            i = int(s)
            if 0 <= i < len(state.monitors):
                # edit flow will manage its own UI
                edit_monitor_flow(state, i)
            else:
                print(f"{YELLOW}Index out of range.{RESET}")
                time.sleep(0.7)
        elif cmd == "r":
            if not state.monitors:
                print(f"{YELLOW}No monitors to remove.{RESET}")
                time.sleep(0.7)
                continue
            s = input("Index to remove: ").strip()
            try:
                i = int(s)
                if 0 <= i < len(state.monitors):
                    removed = state.monitors.pop(i)
                    save_state(state)
                    print(f"{GREEN}Removed {removed.url} ({removed.selector or 'whole page'}).{RESET}")
                    time.sleep(0.7)
                else:
                    print(f"{YELLOW}Index out of range.{RESET}")
                    time.sleep(0.7)
            except Exception:
                print(f"{YELLOW}Invalid index.{RESET}")
                time.sleep(0.7)
        elif cmd == "v":
            if not state.monitors:
                print(f"{YELLOW}No monitors to preview.{RESET}")
                time.sleep(0.7)
                continue
            s = input("Index to preview: ").strip()
            if not s.isdigit():
                print(f"{YELLOW}Enter a valid index.{RESET}")
                time.sleep(0.7)
                continue
            i = int(s)
            if not (0 <= i < len(state.monitors)):
                print(f"{YELLOW}Index out of range.{RESET}")
                time.sleep(0.7)
                continue
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
            print(f"{YELLOW}Unknown choice.{RESET}")
            time.sleep(0.7)

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
    table.add_column("Menu Options", style="bold white")
    table.add_row("1", "Manage monitors (Add/Edit/Remove/List)")
    table.add_row("2", "Set Discord webhook")
    table.add_row("3", "Start monitoring")
    table.add_row("4", f"Toggle status heartbeat (currently {'ON' if state.verbose_status else 'OFF'})")
    table.add_row("5", "Choose default User-Agent (presets)")
    table.add_row("6", "Quit")
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
            print("1) Manage monitors (Add/Edit/Remove/List)")
            print("2) Set Discord webhook")
            print("3) Start monitoring")
            print(f"4) Toggle status heartbeat (currently {'ON' if state.verbose_status else 'OFF'})")
            print("5) Choose default User-Agent (presets)")
            print("6) Quit")
        choice = input("\nSelect: ").strip()
        clear()
        if choice == "1":
            manage_monitors_flow(state)
        elif choice == "2":
            set_webhook_flow(state)
            input("Press Enter to continue...")
        elif choice == "3":
            start_monitoring(state)
        elif choice == "4":
            toggle_verbose_flow(state)
        elif choice == "5":
            choose_user_agent_flow(state)
        elif choice == "6":
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
