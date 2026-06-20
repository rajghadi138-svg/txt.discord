#!/usr/bin/env python3
"""
Dispatcher — Discord self-bot auto-poster with a built-in web control panel.

⚠️  WARNING: Self-bots violate Discord's Terms of Service.
    Using your user token can result in a permanent account ban.
    Only use this on servers you own, and understand the risks.

A self-bot that posts lines from a text file into a configured channel,
paced by a random delay. Everything is controlled from a local web dashboard:
upload / edit / create message files, pick which file is active, set the
channel and delay, arm / disarm posting, toggle the feed.

Setup:
    python -m venv .venv
    source .venv/bin/activate.fish  # or .venv/bin/activate
    pip install -U discord.py-self flask

    # Get your user token (BROWSER METHOD - use at your own risk):
    # 1. Open Discord in browser
    # 2. Press F12 → Application → Local Storage → discord.com
    # 3. Copy the "token" value
    # set -x DISCORD_TOKEN "your_user_token"

Run:
    python app.py
    # open http://127.0.0.1:5000
"""
import os
import sys
import json
import time
import random
import secrets
import threading
from pathlib import Path
from datetime import datetime

import discord
from discord.ext import commands, tasks
from flask import Flask, request, jsonify, render_template_string, abort, Response

# ---- paths --------------------------------------------------------------
BASE      = Path(__file__).resolve().parent
MSG_DIR   = BASE / "messages"
CONFIG    = BASE / "config.json"
STATUS    = BASE / "status.json"
MSG_DIR.mkdir(exist_ok=True)

_lock = threading.Lock()

DEFAULT_CONFIG = {
    "channel_id": None,
    "active_file": None,
    "delay_min": 6.0,
    "delay_max": 7.0,
    "loop": True,
    "running": False,
    "feed": True,
    "autostart": False,
}


# ---- state helpers ------------------------------------------------------
def read_config() -> dict:
    with _lock:
        if not CONFIG.exists():
            CONFIG.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
            return dict(DEFAULT_CONFIG)
        cfg = json.loads(CONFIG.read_text())
    merged = {**DEFAULT_CONFIG, **cfg}
    return merged


def write_config(cfg: dict) -> None:
    with _lock:
        CONFIG.write_text(json.dumps(cfg, indent=2))


def write_status(status: dict) -> None:
    with _lock:
        STATUS.write_text(json.dumps(status, indent=2))


def read_status() -> dict:
    with _lock:
        if not STATUS.exists():
            return {}
        return json.loads(STATUS.read_text())


def safe_name(name: str) -> str:
    name = os.path.basename(name.strip())
    if not name or name in (".", ".."):
        abort(400, "bad filename")
    if not name.endswith(".txt"):
        name += ".txt"
    return name


def load_lines(filename: str) -> list[str]:
    path = MSG_DIR / filename
    if not path.exists():
        return []
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    return [ln for ln in lines if ln]


# =========================================================================
#  Self-Bot Cog — NO prefix commands, dashboard-only control
# =========================================================================
class DispatcherCog(commands.Cog):
    """
    Self-bot cog that handles the auto-poster loop.
    NO prefix commands — everything controlled via the web dashboard.
    """
    FEED_CAP = 40

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._messages: list[str] = []
        self._index: int = 0
        self._next_at: float = 0.0
        self._was_running: bool = False
        self._active_file: str | None = None
        self._feed: list[dict] = []
        self._error: str | None = None

        self.poster.start()

    def cog_unload(self):
        self.poster.cancel()

    def push_status(self):
        cfg = read_config()
        write_status({
            "connected": self.bot.is_ready(),
            "bot_name": str(self.bot.user) if self.bot.user else None,
            "running": cfg["running"],
            "active_file": self._active_file,
            "index": self._index,
            "total": len(self._messages),
            "error": self._error,
            "feed": self._feed[-self.FEED_CAP:] if cfg["feed"] else [],
            "updated": datetime.now().strftime("%H:%M:%S"),
        })

    @tasks.loop(seconds=1.0)
    async def poster(self):
        cfg = read_config()
        now = time.monotonic()

        if not cfg["running"]:
            if self._was_running:
                self._was_running = False
                self.push_status()
            return

        fresh_start = not self._was_running
        file_changed = cfg["active_file"] != self._active_file
        if fresh_start or file_changed:
            self._active_file = cfg["active_file"]
            self._messages = load_lines(cfg["active_file"]) if cfg["active_file"] else []
            self._index = 0
            self._next_at = now
            self._was_running = True
            self._error = None
            if not self._messages:
                self._error = "active file is empty or unset — nothing to post"
                cfg["running"] = False
                write_config(cfg)
                self.push_status()
                return

        if now < self._next_at:
            return

        if self._index >= len(self._messages):
            if cfg["loop"]:
                self._index = 0
            else:
                cfg["running"] = False
                write_config(cfg)
                self._was_running = False
                self.push_status()
                return

        channel = self.bot.get_channel(cfg["channel_id"]) if cfg["channel_id"] else None
        if channel is None:
            self._error = "channel not found — check the channel ID / bot access"
            cfg["running"] = False
            write_config(cfg)
            self.push_status()
            return

        line = self._messages[self._index]
        try:
            await channel.send(line)
            self._error = None
            if cfg["feed"]:
                self._feed.append({
                    "t": datetime.now().strftime("%H:%M:%S"),
                    "text": line[:200],
                })
                self._feed = self._feed[-self.FEED_CAP:]
        except discord.Forbidden:
            self._error = "missing permission to send in that channel"
            cfg["running"] = False
            write_config(cfg)
            self.push_status()
            return
        except discord.HTTPException as e:
            self._error = f"send failed (rate-limited?): {e}"
            self._next_at = now + 10
            self.push_status()
            return

        self._index += 1
        self._next_at = now + random.uniform(cfg["delay_min"], cfg["delay_max"])
        self.push_status()

    @poster.before_loop
    async def before_poster(self):
        await self.bot.wait_until_ready()


# =========================================================================
#  Self-Bot class — prefix required by discord.py-self but we ignore it
# =========================================================================
class SelfBot(commands.Bot):
    def __init__(self):
        # discord.py-self requires command_prefix even for self_bots.
        # "\\" is a single backslash character — nobody starts a message
        # with it, so prefix commands are effectively disabled.
        super().__init__(
            command_prefix="\\",  # single backslash prefix = no commands
            self_bot=True,
        )

    async def setup_hook(self):
        await self.add_cog(DispatcherCog(self))
        print(f"Self-bot loaded. Logged in as {self.user} ({self.user.id})")

    async def on_ready(self):
        print(f"Ready! {self.user} is online.")


# =========================================================================
#  Dashboard HTML (embedded — no external files needed)
# =========================================================================
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dispatcher — Self-Bot Control Panel</title>
    <style>
        :root {
            --bg: #0f0f1a;
            --surface: #1a1a2e;
            --surface-hover: #252542;
            --border: #2d2d44;
            --text: #e0e0e0;
            --text-muted: #8888a0;
            --accent: #5865f2;
            --accent-hover: #4752c4;
            --danger: #ed4245;
            --danger-hover: #c03537;
            --success: #57f287;
            --warning: #fee75c;
            --radius: 8px;
            --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: var(--bg);
            color: var(--text);
            font-family: var(--font);
            line-height: 1.5;
            min-height: 100vh;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        header {
            text-align: center;
            padding: 30px 0;
            border-bottom: 1px solid var(--border);
            margin-bottom: 30px;
        }
        header h1 { font-size: 2rem; color: var(--accent); margin-bottom: 5px; }
        header p { color: var(--text-muted); font-size: 0.9rem; }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }
        .card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 20px;
        }
        .card h2 {
            font-size: 1.1rem;
            color: var(--accent);
            margin-bottom: 15px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .status-indicator {
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: var(--danger);
            margin-left: auto;
        }
        .status-indicator.on { background: var(--success); box-shadow: 0 0 8px var(--success); }
        .status-indicator.warn { background: var(--warning); }
        .form-group { margin-bottom: 15px; }
        label {
            display: block;
            font-size: 0.85rem;
            color: var(--text-muted);
            margin-bottom: 5px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        input, select, textarea {
            width: 100%;
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            color: var(--text);
            padding: 10px 12px;
            font-family: inherit;
            font-size: 0.95rem;
            transition: border-color 0.2s;
        }
        input:focus, select:focus, textarea:focus {
            outline: none;
            border-color: var(--accent);
        }
        textarea {
            min-height: 200px;
            resize: vertical;
            font-family: 'Consolas', 'Monaco', monospace;
            font-size: 0.85rem;
            line-height: 1.6;
        }
        .row { display: flex; gap: 10px; }
        .row .form-group { flex: 1; }
        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            padding: 10px 18px;
            border: none;
            border-radius: var(--radius);
            font-family: inherit;
            font-size: 0.9rem;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.15s;
            color: white;
        }
        .btn-primary { background: var(--accent); }
        .btn-primary:hover { background: var(--accent-hover); }
        .btn-danger { background: var(--danger); }
        .btn-danger:hover { background: var(--danger-hover); }
        .btn-success { background: #248046; }
        .btn-success:hover { background: #1a6334; }
        .btn-sm { padding: 6px 12px; font-size: 0.8rem; }
        .btn-group { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
        .file-list {
            max-height: 300px;
            overflow-y: auto;
        }
        .file-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px 12px;
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            margin-bottom: 8px;
            cursor: pointer;
            transition: all 0.15s;
        }
        .file-item:hover { background: var(--surface-hover); }
        .file-item.active {
            border-color: var(--accent);
            background: rgba(88, 101, 242, 0.1);
        }
        .file-item .name { font-weight: 500; }
        .file-item .meta { font-size: 0.8rem; color: var(--text-muted); }
        .file-item .actions { display: flex; gap: 5px; }
        .feed {
            max-height: 300px;
            overflow-y: auto;
            font-family: 'Consolas', monospace;
            font-size: 0.8rem;
        }
        .feed-item {
            padding: 8px 10px;
            border-bottom: 1px solid var(--border);
            display: flex;
            gap: 10px;
        }
        .feed-item .time { color: var(--text-muted); min-width: 60px; }
        .feed-item .text { color: var(--text); word-break: break-word; }
        .feed-empty { color: var(--text-muted); text-align: center; padding: 20px; }
        .error-box {
            background: rgba(237, 66, 69, 0.1);
            border: 1px solid var(--danger);
            color: var(--danger);
            padding: 10px 12px;
            border-radius: var(--radius);
            font-size: 0.85rem;
            margin-top: 10px;
        }
        .progress-bar {
            width: 100%;
            height: 6px;
            background: var(--bg);
            border-radius: 3px;
            margin-top: 10px;
            overflow: hidden;
        }
        .progress-bar .fill {
            height: 100%;
            background: var(--accent);
            border-radius: 3px;
            transition: width 0.3s;
        }
        .stats {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 10px;
            margin-top: 15px;
        }
        .stat {
            text-align: center;
            padding: 10px;
            background: var(--bg);
            border-radius: var(--radius);
        }
        .stat .value { font-size: 1.3rem; font-weight: 600; color: var(--accent); }
        .stat .label { font-size: 0.75rem; color: var(--text-muted); margin-top: 2px; }
        .hidden { display: none; }
        .drop-zone {
            border: 2px dashed var(--border);
            border-radius: var(--radius);
            padding: 30px;
            text-align: center;
            color: var(--text-muted);
            transition: all 0.2s;
            cursor: pointer;
        }
        .drop-zone:hover, .drop-zone.dragover {
            border-color: var(--accent);
            background: rgba(88, 101, 242, 0.05);
        }
        input[type="file"] { display: none; }
        ::-webkit-scrollbar { width: 8px; }
        ::-webkit-scrollbar-track { background: var(--bg); }
        ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🚀 Dispatcher</h1>
            <p>Self-Bot Control Panel — <span id="conn-status">connecting...</span></p>
        </header>

        <div class="grid">
            <div class="card">
                <h2>
                    Status
                    <span id="status-dot" class="status-indicator"></span>
                </h2>
                <div class="stats">
                    <div class="stat">
                        <div class="value" id="stat-index">0</div>
                        <div class="label">Index</div>
                    </div>
                    <div class="stat">
                        <div class="value" id="stat-total">0</div>
                        <div class="label">Total</div>
                    </div>
                    <div class="stat">
                        <div class="value" id="stat-progress">0%</div>
                        <div class="label">Progress</div>
                    </div>
                </div>
                <div class="progress-bar">
                    <div class="fill" id="progress-fill" style="width: 0%"></div>
                </div>
                <div id="error-box" class="error-box hidden"></div>
                <div class="btn-group">
                    <button class="btn btn-success" id="btn-start" onclick="control('start')">▶ Start</button>
                    <button class="btn btn-danger" id="btn-stop" onclick="control('stop')">⏹ Stop</button>
                </div>
            </div>

            <div class="card">
                <h2>⚙️ Configuration</h2>
                <div class="form-group">
                    <label>Channel ID</label>
                    <input type="text" id="channel-id" placeholder="123456789012345678">
                </div>
                <div class="row">
                    <div class="form-group">
                        <label>Delay Min (sec)</label>
                        <input type="number" id="delay-min" value="6" min="1" step="0.5">
                    </div>
                    <div class="form-group">
                        <label>Delay Max (sec)</label>
                        <input type="number" id="delay-max" value="7" min="1" step="0.5">
                    </div>
                </div>
                <div class="row" style="margin-top: 10px;">
                    <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                        <input type="checkbox" id="loop-check" checked>
                        <span>Loop</span>
                    </label>
                    <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                        <input type="checkbox" id="feed-check" checked>
                        <span>Feed</span>
                    </label>
                    <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                        <input type="checkbox" id="autostart-check">
                        <span>Auto-start</span>
                    </label>
                </div>
                <button class="btn btn-primary" style="width: 100%; margin-top: 15px;" onclick="saveConfig()">💾 Save Config</button>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <h2>📁 Message Files</h2>
                <div class="drop-zone" id="drop-zone" onclick="document.getElementById('file-input').click()">
                    <p>📎 Click to upload or drop a .txt file here</p>
                    <input type="file" id="file-input" accept=".txt" onchange="uploadFile(this)">
                </div>
                <div class="file-list" id="file-list" style="margin-top: 15px;">
                    <p class="feed-empty">Loading files...</p>
                </div>
                <div class="btn-group">
                    <button class="btn btn-primary btn-sm" onclick="createNewFile()">➕ New File</button>
                </div>
            </div>

            <div class="card" id="editor-card" style="display: none;">
                <h2>✏️ Editor: <span id="editor-filename">untitled.txt</span></h2>
                <textarea id="editor-content" placeholder="Enter your messages here, one per line..."></textarea>
                <div class="btn-group">
                    <button class="btn btn-success btn-sm" onclick="saveFile()">💾 Save</button>
                    <button class="btn btn-danger btn-sm" onclick="deleteFile()">🗑 Delete</button>
                    <button class="btn btn-primary btn-sm" onclick="setActive()">⭐ Set Active</button>
                    <button class="btn btn-sm" style="background: var(--border);" onclick="closeEditor()">✕ Close</button>
                </div>
            </div>

            <div class="card">
                <h2>📡 Live Feed</h2>
                <div class="feed" id="feed">
                    <p class="feed-empty">No messages sent yet.</p>
                </div>
            </div>
        </div>
    </div>

    <script>
        let currentFile = null;
        let files = [];
        let config = {};

        setInterval(fetchStatus, 1000);
        setInterval(fetchFiles, 5000);
        fetchStatus();
        fetchFiles();
        fetchConfig();

        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                updateStatus(data);
            } catch (e) {
                document.getElementById('conn-status').textContent = 'disconnected';
                document.getElementById('conn-status').style.color = 'var(--danger)';
            }
        }

        async function fetchConfig() {
            try {
                const res = await fetch('/api/config');
                config = await res.json();
                document.getElementById('channel-id').value = config.channel_id || '';
                document.getElementById('delay-min').value = config.delay_min;
                document.getElementById('delay-max').value = config.delay_max;
                document.getElementById('loop-check').checked = config.loop;
                document.getElementById('feed-check').checked = config.feed;
                document.getElementById('autostart-check').checked = config.autostart;
            } catch (e) { console.error('fetchConfig:', e); }
        }

        async function fetchFiles() {
            try {
                const res = await fetch('/api/files');
                const data = await res.json();
                files = data.files;
                renderFiles();
            } catch (e) { console.error('fetchFiles:', e); }
        }

        function updateStatus(data) {
            document.getElementById('conn-status').textContent = data.connected ? 'connected' : 'disconnected';
            document.getElementById('conn-status').style.color = data.connected ? 'var(--success)' : 'var(--danger)';

            const dot = document.getElementById('status-dot');
            if (data.running) {
                dot.className = 'status-indicator on';
            } else if (data.error) {
                dot.className = 'status-indicator warn';
            } else {
                dot.className = 'status-indicator';
            }

            document.getElementById('stat-index').textContent = data.index || 0;
            document.getElementById('stat-total').textContent = data.total || 0;
            const pct = data.total ? Math.round((data.index / data.total) * 100) : 0;
            document.getElementById('stat-progress').textContent = pct + '%';
            document.getElementById('progress-fill').style.width = pct + '%';

            const errBox = document.getElementById('error-box');
            if (data.error) {
                errBox.textContent = '⚠️ ' + data.error;
                errBox.classList.remove('hidden');
            } else {
                errBox.classList.add('hidden');
            }

            const feedEl = document.getElementById('feed');
            if (data.feed && data.feed.length > 0) {
                feedEl.innerHTML = data.feed.slice().reverse().map(item => `
                    <div class="feed-item">
                        <span class="time">${item.t}</span>
                        <span class="text">${escapeHtml(item.text)}</span>
                    </div>
                `).join('');
            } else {
                feedEl.innerHTML = '<p class="feed-empty">No messages sent yet.</p>';
            }

            if (data.active_file) {
                document.querySelectorAll('.file-item').forEach(el => {
                    el.classList.toggle('active', el.dataset.name === data.active_file);
                });
            }
        }

        function renderFiles() {
            const list = document.getElementById('file-list');
            if (files.length === 0) {
                list.innerHTML = '<p class="feed-empty">No files yet. Upload or create one.</p>';
                return;
            }
            list.innerHTML = files.map(f => `
                <div class="file-item" data-name="${f.name}" onclick="openFile('${f.name}')">
                    <div>
                        <div class="name">${f.name}</div>
                        <div class="meta">${f.lines} lines</div>
                    </div>
                    <div class="actions" onclick="event.stopPropagation()">
                        <button class="btn btn-primary btn-sm" onclick="quickSetActive('${f.name}')">⭐</button>
                    </div>
                </div>
            `).join('');
        }

        async function openFile(name) {
            try {
                const res = await fetch(`/api/file/${encodeURIComponent(name)}`);
                const data = await res.json();
                currentFile = data.name;
                document.getElementById('editor-filename').textContent = data.name;
                document.getElementById('editor-content').value = data.content;
                document.getElementById('editor-card').style.display = 'block';
            } catch (e) { alert('Failed to load file'); }
        }

        function closeEditor() {
            document.getElementById('editor-card').style.display = 'none';
            currentFile = null;
        }

        async function saveFile() {
            if (!currentFile) return;
            const content = document.getElementById('editor-content').value;
            try {
                await fetch(`/api/file/${encodeURIComponent(currentFile)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ content })
                });
                fetchFiles();
            } catch (e) { alert('Failed to save'); }
        }

        async function deleteFile() {
            if (!currentFile) return;
            if (!confirm(`Delete ${currentFile}?`)) return;
            try {
                await fetch(`/api/file/${encodeURIComponent(currentFile)}`, { method: 'DELETE' });
                closeEditor();
                fetchFiles();
            } catch (e) { alert('Failed to delete'); }
        }

        async function setActive() {
            if (!currentFile) return;
            await saveConfig(currentFile);
        }

        async function quickSetActive(name) {
            await saveConfig(name);
        }

        async function saveConfig(activeFile = null) {
            const payload = {
                channel_id: document.getElementById('channel-id').value,
                delay_min: parseFloat(document.getElementById('delay-min').value),
                delay_max: parseFloat(document.getElementById('delay-max').value),
                loop: document.getElementById('loop-check').checked,
                feed: document.getElementById('feed-check').checked,
                autostart: document.getElementById('autostart-check').checked,
            };
            if (activeFile) payload.active_file = activeFile;

            try {
                await fetch('/api/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                fetchConfig();
                fetchFiles();
            } catch (e) { alert('Failed to save config'); }
        }

        async function createNewFile() {
            const name = prompt('Enter filename (without .txt):');
            if (!name) return;
            const fullName = name.endsWith('.txt') ? name : name + '.txt';
            try {
                await fetch(`/api/file/${encodeURIComponent(fullName)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ content: '' })
                });
                fetchFiles();
                openFile(fullName);
            } catch (e) { alert('Failed to create file'); }
        }

        async function uploadFile(input) {
            const file = input.files[0];
            if (!file) return;
            const form = new FormData();
            form.append('file', file);
            try {
                await fetch('/api/upload', { method: 'POST', body: form });
                input.value = '';
                fetchFiles();
            } catch (e) { alert('Failed to upload'); }
        }

        async function control(action) {
            try {
                const res = await fetch('/api/control', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action })
                });
                const data = await res.json();
                if (!data.ok) alert(data.error);
                fetchStatus();
            } catch (e) { alert('Control failed'); }
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        const dropZone = document.getElementById('drop-zone');
        dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('dragover'); });
        dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
        dropZone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropZone.classList.remove('dragover');
            const file = e.dataTransfer.files[0];
            if (file && file.name.endsWith('.txt')) {
                const input = document.getElementById('file-input');
                const dt = new DataTransfer();
                dt.items.add(file);
                input.files = dt.files;
                uploadFile(input);
            }
        });
    </script>
</body>
</html>
"""


# =========================================================================
#  Flask dashboard
# =========================================================================
app = Flask(__name__)

# Optional auth gate — protects the panel + API when exposed publicly (e.g. on
# Render). Set DASHBOARD_PASSWORD to enable it; leave it unset for an open panel
# (fine for local dev). /healthz stays open so uptime monitors can ping it.
DASH_PASSWORD = os.getenv("DASHBOARD_PASSWORD")


@app.before_request
def _require_auth():
    if request.path == "/healthz":
        return  # keep-alive / health check — always open
    if not DASH_PASSWORD:
        return  # auth disabled
    auth = request.authorization
    if auth and secrets.compare_digest(auth.password or "", DASH_PASSWORD):
        return  # authorized
    return Response(
        "Authentication required.",
        401,
        {"WWW-Authenticate": 'Basic realm="Dispatcher"'},
    )


@app.get("/healthz")
def healthz():
    return "ok", 200


@app.get("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.get("/api/files")
def api_files():
    files = sorted(p.name for p in MSG_DIR.glob("*.txt"))
    return jsonify({
        "files": [{"name": n, "lines": len(load_lines(n))} for n in files]
    })


@app.get("/api/file/<name>")
def api_file_get(name):
    name = safe_name(name)
    path = MSG_DIR / name
    if not path.exists():
        abort(404)
    return jsonify({"name": name, "content": path.read_text(encoding="utf-8")})


@app.post("/api/file/<name>")
def api_file_save(name):
    name = safe_name(name)
    content = request.json.get("content", "")
    (MSG_DIR / name).write_text(content, encoding="utf-8")
    return jsonify({"ok": True, "name": name, "lines": len(load_lines(name))})


@app.delete("/api/file/<name>")
def api_file_delete(name):
    name = safe_name(name)
    path = MSG_DIR / name
    if path.exists():
        path.unlink()
    cfg = read_config()
    if cfg["active_file"] == name:
        cfg["active_file"] = None
        cfg["running"] = False
        write_config(cfg)
    return jsonify({"ok": True})


@app.post("/api/upload")
def api_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400, "no file")
    name = safe_name(f.filename)
    (MSG_DIR / name).write_text(f.read().decode("utf-8", "replace"), encoding="utf-8")
    return jsonify({"ok": True, "name": name, "lines": len(load_lines(name))})


@app.get("/api/config")
def api_config_get():
    return jsonify(read_config())


@app.post("/api/config")
def api_config_set():
    cfg = read_config()
    data = request.json or {}
    if "channel_id" in data:
        raw = str(data["channel_id"]).strip()
        cfg["channel_id"] = int(raw) if raw.isdigit() else None
    if "active_file" in data:
        cfg["active_file"] = safe_name(data["active_file"]) if data["active_file"] else None
    for key in ("delay_min", "delay_max"):
        if key in data:
            cfg[key] = max(1.0, float(data[key]))
    if cfg["delay_max"] < cfg["delay_min"]:
        cfg["delay_max"] = cfg["delay_min"]
    for key in ("loop", "feed", "autostart"):
        if key in data:
            cfg[key] = bool(data[key])
    write_config(cfg)
    return jsonify(cfg)


@app.post("/api/control")
def api_control():
    action = (request.json or {}).get("action")
    cfg = read_config()
    if action == "start":
        if not cfg["active_file"]:
            return jsonify({"ok": False, "error": "pick an active file first"}), 400
        if not cfg["channel_id"]:
            return jsonify({"ok": False, "error": "set a channel ID first"}), 400
        cfg["running"] = True
    elif action == "stop":
        cfg["running"] = False
    else:
        abort(400, "unknown action")
    write_config(cfg)
    return jsonify({"ok": True, "running": cfg["running"]})


@app.get("/api/status")
def api_status():
    return jsonify(read_status())


# =========================================================================
#  Boot
# =========================================================================
def run_flask():
    port = int(os.getenv("PORT", "5000"))
    host = os.getenv("HOST", "0.0.0.0")  # 0.0.0.0 so Render can detect the port
    app.run(host=host, port=port, debug=False, use_reloader=False)


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        sys.exit('Set DISCORD_TOKEN.  fish:  set -x DISCORD_TOKEN "your_token"')

    if not CONFIG.exists():
        write_config(dict(DEFAULT_CONFIG))

    # Auto-start: if enabled and prerequisites are set, begin posting on boot.
    # Re-applies on every process start (e.g. after a Render restart), so the
    # bot resumes by itself. A manual Stop during a session still sticks,
    # because this runs once at boot — not on reconnects.
    cfg = read_config()
    if cfg.get("autostart") and cfg.get("active_file") and cfg.get("channel_id"):
        cfg["running"] = True
        write_config(cfg)
        print("Auto-start enabled — posting begins once connected.")

    threading.Thread(target=run_flask, daemon=True).start()
    port = os.getenv("PORT", "5000")
    print("=" * 50)
    print(f"Dashboard listening on 0.0.0.0:{port}")
    if DASH_PASSWORD:
        print("Dashboard auth: ENABLED (DASHBOARD_PASSWORD is set)")
    else:
        print("Dashboard auth: OFF — set DASHBOARD_PASSWORD to lock it down")
    print("=" * 50)

    bot = SelfBot()
    bot.run(token)


if __name__ == "__main__":
    main()
