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
PURPLE  = "\033[38;2;150;82;214m"   # royal purple (headers/info)
RESET = "\033[0m"

# ### ADDED ### common real-browsers UA presets
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
            colored.append(PURPLE + ln + RESET)
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
                            self.alert_queue.put(f"{PURPLE}[{ts}] {self.monitor.url} — no change (304).{RESET}")
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

                            # emit URL + diff as ONE clean block (no stray blank line)
                            combined = (
                                f"{RED}[CHANGE {ts_hms}] {self.monitor.url} — {self._selector_label()} changed! {RESET}\n"
                                f"{PURPLE}{self.monitor.url}{RESET}\n"
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
                                self.alert_queue.put(f"{PURPLE}[{ts}] {self.monitor.url} — checked, no change.{RESET}")

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
    sys.stdout.write("\x1b[8;{rows};{cols}t".format(rows=32, cols=130))
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
            print(f"{PURPLE}Previewing selector `{selector}` (mode={mode}, JS={use_js})...{RESET}")
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
        print(f"{PURPLE}JS-rendered mode enabled (wait {js_wait_ms} ms; wait_for: {js_wait_selector or 'None'}).{RESET}")

def remove_monitor_flow(state: AppState) -> None:
    if not state.monitors:
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

def list_monitors(state: AppState) -> None:
    if not state.monitors:
        print(f"{YELLOW}No monitors configured yet.{RESET}")
        return
    for i, m in enumerate(state.monitors):
        print(f"[{i}] {m.url}  | selector: {m.selector or '(whole page)'} | mode: {m.mode} | every {m.interval_sec}s | use_js={m.use_js}")

def set_webhook_flow(state: AppState) -> None:
    current = state.webhook_url or "(not set)"
    print(f"{PURPLE}Current Discord webhook: {current}{RESET}")
    new_url = input("Enter Discord webhook URL (or blank to keep): ").strip()
    if new_url:
        state.webhook_url = new_url
        save_state(state)
        print(f"{GREEN}Webhook updated.{RESET}")

def toggle_verbose_flow(state: AppState) -> None:
    state.verbose_status = not state.verbose_status
    save_state(state)
    print(f"{GREEN}Verbose status is now {'ON' if state.verbose_status else 'OFF'}.{RESET}")

# pick a preset real-world User-Agent as the global default
def choose_user_agent_flow(state: AppState) -> None:
    global DEFAULT_USER_AGENT
    print(f"{PURPLE}Select a default User-Agent (used unless a monitor overrides it):{RESET}")
    keys = list(PRESET_USER_AGENTS.keys())
    for idx, name in enumerate(keys):
        marker = " (current)" if PRESET_USER_AGENTS[name] == (state.default_user_agent or DEFAULT_USER_AGENT) else ""
        print(f"  {idx}) {name}{marker}")
    s = input("Choice (number), or leave blank to cancel: ").strip()
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
            print(f"{PURPLE}Discord webhook is set: alerts will also be sent to your channel.{RESET}")
        else:
            print(f"{YELLOW}Discord webhook not set; alerts will appear only in this window.{RESET}")
        print(f"{PURPLE}Tip: For JS dashboards, enable JS-rendered mode on that monitor.{RESET}")
        if state.verbose_status:
            print(f"{PURPLE}Verbose status heartbeat is ON. Toggle from the main menu if noisy.{RESET}")
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
# Main menu
# ========================

def main_menu():
    state = load_state()
    if not state.webhook_url:
        ensure_webhook_set(state)

    while True:
        clear()
        stdout.write(print_header())
        print("1) Add a monitor")
        print("2) Remove a monitor")
        print("3) List monitors")
        print("4) Set Discord webhook")
        print("5) Start monitoring")
        print(f"6) Toggle status heartbeat (currently {'ON' if state.verbose_status else 'OFF'})")
        print("7) Choose default User-Agent (presets)")
        print("8) Quit")
        choice = input("\nSelect: ").strip()
        clear()
        if choice == "1":
            add_monitor_flow(state)
            input("Press Enter to continue...")
        elif choice == "2":
            remove_monitor_flow(state)
            input("Press Enter to continue...")
        elif choice == "3":
            list_monitors(state)
            input("Press Enter to continue...")
        elif choice == "4":
            set_webhook_flow(state)
            input("Press Enter to continue...")
        elif choice == "5":
            start_monitoring(state)
        elif choice == "6":
            toggle_verbose_flow(state)
        elif choice == "7":
            choose_user_agent_flow(state)
        elif choice == "8":
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
