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
        if channel is None and cfg["channel_id"]:
            # Not in cache — resolve it directly via the API. Catches cache-timing
            # issues, and gives a clearer reason when it genuinely can't be reached.
            try:
                channel = await self.bot.fetch_channel(cfg["channel_id"])
            except discord.NotFound:
                self._error = ("channel ID not found — double-check the ID, and make "
                               "sure this account is a member of that server")
            except discord.Forbidden:
                self._error = "no access to that channel — this account can't view it"
            except discord.HTTPException as e:
                self._error = f"couldn't resolve channel: {e}"
        if channel is None:
            if not self._error:
                self._error = "channel not found — set a valid channel ID"
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
<title>Dispatcher · Control Panel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{
  --base:#1e1e2e; --mantle:#181825; --crust:#11111b;
  --surface0:#313244; --surface1:#45475a; --surface2:#585b70;
  --overlay0:#6c7086; --overlay1:#7f849c;
  --subtext0:#a6adc8; --subtext1:#bac2de; --text:#cdd6f4;
  --mauve:#cba6f7; --lavender:#b4befe; --blue:#89b4fa; --sapphire:#74c7ec; --teal:#94e2d5;
  --green:#a6e3a1; --yellow:#f9e2af; --peach:#fab387; --red:#f38ba8; --pink:#f5c2e7;
  --glass: rgba(49,50,68,0.42);
  --glass-line: rgba(203,166,247,0.14);
  --radius: 18px;
  --shadow: 0 10px 34px rgba(17,17,27,0.5), inset 0 1px 0 rgba(255,255,255,0.04);
  --display:'Space Grotesk', sans-serif;
  --body:'Inter', sans-serif;
  --mono:'JetBrains Mono', monospace;
}
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  background:var(--crust); color:var(--text);
  font-family:var(--body); line-height:1.5; min-height:100vh;
  -webkit-font-smoothing:antialiased;
}
body::before{
  content:''; position:fixed; inset:-10%; z-index:-1;
  background:
    radial-gradient(55% 45% at 12% 8%,  rgba(203,166,247,0.20), transparent 62%),
    radial-gradient(50% 42% at 88% 12%, rgba(137,180,250,0.18), transparent 62%),
    radial-gradient(55% 50% at 82% 92%, rgba(148,226,213,0.13), transparent 62%),
    radial-gradient(50% 45% at 8% 90%,  rgba(245,194,231,0.13), transparent 62%),
    var(--crust);
  animation:drift 24s ease-in-out infinite alternate;
}
@keyframes drift{from{transform:translate3d(0,0,0) scale(1)}to{transform:translate3d(0,-2.5%,0) scale(1.06)}}
@media (prefers-reduced-motion:reduce){body::before{animation:none}}

.wrap{max-width:1240px;margin:0 auto;padding:28px 22px 64px}

/* ---- header ---- */
header{
  display:flex;justify-content:space-between;align-items:flex-start;gap:24px;
  padding:22px 26px;margin-bottom:26px;
  background:var(--glass);backdrop-filter:blur(18px) saturate(150%);-webkit-backdrop-filter:blur(18px) saturate(150%);
  border:1px solid var(--glass-line);border-radius:var(--radius);box-shadow:var(--shadow);
}
.brand{display:flex;align-items:center;gap:14px}
.logo{
  width:46px;height:46px;border-radius:14px;flex:none;
  background:linear-gradient(135deg,var(--mauve),var(--blue));
  display:grid;place-items:center;font-size:22px;
  box-shadow:0 6px 18px rgba(203,166,247,0.35);
}
.brand h1{font-family:var(--display);font-size:1.5rem;font-weight:700;letter-spacing:-0.02em}
.brand p{font-family:var(--mono);font-size:0.72rem;color:var(--overlay1);letter-spacing:0.08em;text-transform:uppercase;margin-top:2px}
.head-right{display:flex;flex-direction:column;align-items:flex-end;gap:10px}
.clock{font-family:var(--mono);font-weight:700;font-size:2.1rem;line-height:1;color:var(--mauve);letter-spacing:0.04em;text-shadow:0 0 22px rgba(203,166,247,0.45)}
.clock-date{font-family:var(--mono);font-size:0.74rem;color:var(--subtext0);letter-spacing:0.12em;text-transform:uppercase;margin-top:-4px}
.pill{display:inline-flex;align-items:center;gap:8px;padding:6px 13px;border-radius:999px;font-family:var(--mono);font-size:0.78rem;font-weight:500;border:1px solid transparent}
.pill::before{content:'';width:8px;height:8px;border-radius:50%}
.pill-on{color:var(--green);background:rgba(166,227,161,0.1);border-color:rgba(166,227,161,0.28)}
.pill-on::before{background:var(--green);box-shadow:0 0 9px var(--green)}
.pill-off{color:var(--red);background:rgba(243,139,168,0.1);border-color:rgba(243,139,168,0.28)}
.pill-off::before{background:var(--red)}

/* ---- grid + cards ---- */
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:22px;margin-bottom:22px}
.card{
  background:var(--glass);backdrop-filter:blur(16px) saturate(150%);-webkit-backdrop-filter:blur(16px) saturate(150%);
  border:1px solid var(--glass-line);border-radius:var(--radius);box-shadow:var(--shadow);padding:22px;
}
.card h2{font-family:var(--display);font-size:1.05rem;font-weight:600;margin-bottom:18px;display:flex;align-items:center;gap:9px}
.card h2 .ic{font-size:1.05rem}
.card h2 .spacer{margin-left:auto}

/* ---- run state ---- */
.run-state{display:flex;align-items:center;gap:11px;padding:13px 16px;border-radius:14px;margin-bottom:18px;border:1px solid transparent}
.run-state .dot{width:11px;height:11px;border-radius:50%;flex:none}
.run-label{font-family:var(--display);font-weight:600;font-size:1.05rem}
.run-sub{font-family:var(--mono);font-size:0.72rem;color:var(--overlay1);margin-left:auto}
.run-state.running{background:rgba(166,227,161,0.09);border-color:rgba(166,227,161,0.25)}
.run-state.running .dot{background:var(--green);box-shadow:0 0 10px var(--green);animation:pulse 1.6s ease-in-out infinite}
.run-state.running .run-label{color:var(--green)}
.run-state.idle{background:rgba(108,112,134,0.12);border-color:rgba(108,112,134,0.25)}
.run-state.idle .dot{background:var(--overlay0)}
.run-state.idle .run-label{color:var(--subtext1)}
.run-state.error{background:rgba(249,226,175,0.09);border-color:rgba(249,226,175,0.25)}
.run-state.error .dot{background:var(--yellow)}
.run-state.error .run-label{color:var(--yellow)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}

/* ---- stats ---- */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:11px;margin-bottom:16px}
.stat{text-align:center;padding:13px 6px;background:rgba(17,17,27,0.32);border:1px solid rgba(203,166,247,0.07);border-radius:13px}
.stat .v{font-family:var(--mono);font-weight:700;font-size:1.4rem;color:var(--lavender)}
.stat .l{font-size:0.66rem;color:var(--overlay1);text-transform:uppercase;letter-spacing:0.08em;margin-top:3px}
.progress{height:7px;background:rgba(17,17,27,0.4);border-radius:99px;overflow:hidden;margin-bottom:16px}
.progress .fill{height:100%;width:0;border-radius:99px;background:linear-gradient(90deg,var(--mauve),var(--sapphire));transition:width .4s ease}

/* ---- error box ---- */
.error-box{display:none;align-items:flex-start;gap:9px;padding:11px 13px;border-radius:12px;margin-bottom:16px;
  background:rgba(243,139,168,0.1);border:1px solid rgba(243,139,168,0.3);color:var(--red);font-size:0.84rem}
.error-box.show{display:flex}
.error-box .err-ic{flex:none}

/* ---- forms ---- */
.field{margin-bottom:15px}
.field label{display:block;font-size:0.72rem;color:var(--subtext0);text-transform:uppercase;letter-spacing:0.07em;margin-bottom:7px}
input[type=text],input[type=number],textarea{
  width:100%;background:rgba(17,17,27,0.42);border:1px solid var(--surface1);border-radius:11px;
  color:var(--text);padding:11px 13px;font-family:var(--mono);font-size:0.9rem;transition:border-color .2s,box-shadow .2s;
}
input::placeholder{color:var(--overlay0)}
input:focus,textarea:focus{outline:none;border-color:var(--mauve);box-shadow:0 0 0 3px rgba(203,166,247,0.16)}
textarea{min-height:230px;resize:vertical;line-height:1.7;font-size:0.85rem}
.two{display:flex;gap:12px}.two .field{flex:1}
.toggles{display:flex;flex-wrap:wrap;gap:18px;margin:6px 0 4px}

/* ---- switch ---- */
.switch{display:inline-flex;align-items:center;gap:9px;cursor:pointer;user-select:none}
.switch input{position:absolute;opacity:0;width:0;height:0}
.track{width:42px;height:24px;border-radius:99px;background:var(--surface1);position:relative;transition:background .2s;flex:none}
.thumb{position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:var(--subtext0);transition:transform .2s,background .2s}
.switch input:checked + .track{background:linear-gradient(135deg,var(--mauve),var(--blue))}
.switch input:checked + .track .thumb{transform:translateX(18px);background:var(--crust)}
.switch input:focus-visible + .track{box-shadow:0 0 0 3px rgba(203,166,247,0.25)}
.switch-label{font-size:0.86rem;color:var(--subtext1)}

/* ---- buttons ---- */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;padding:11px 18px;border:none;border-radius:12px;
  font-family:var(--body);font-size:0.88rem;font-weight:600;cursor:pointer;transition:transform .12s,filter .15s,box-shadow .15s;color:var(--crust)}
.btn:hover{filter:brightness(1.08)}
.btn:active{transform:translateY(1px)}
.btn:focus-visible{outline:none;box-shadow:0 0 0 3px rgba(203,166,247,0.3)}
.btn-primary{background:linear-gradient(135deg,var(--mauve),var(--blue))}
.btn-start{background:linear-gradient(135deg,var(--green),var(--teal))}
.btn-danger{background:linear-gradient(135deg,var(--red),var(--peach))}
.btn-ghost{background:rgba(88,91,112,0.4);color:var(--subtext1)}
.btn-ghost:hover{background:rgba(88,91,112,0.6);filter:none}
.btn.sm{padding:8px 13px;font-size:0.8rem}
.btn.block{width:100%}
.btn-row{display:flex;gap:11px;flex-wrap:wrap;margin-top:14px}

/* ---- drop zone + files ---- */
.drop{border:1.5px dashed var(--surface2);border-radius:13px;padding:26px;text-align:center;color:var(--overlay1);
  font-size:0.86rem;cursor:pointer;transition:border-color .2s,background .2s}
.drop:hover,.drop.over{border-color:var(--mauve);background:rgba(203,166,247,0.06);color:var(--subtext1)}
.drop b{color:var(--mauve);font-weight:600}
input[type=file]{display:none}
.file-list{margin-top:15px;max-height:300px;overflow-y:auto;display:flex;flex-direction:column;gap:8px}
.file-item{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:11px 13px;border-radius:12px;cursor:pointer;
  background:rgba(17,17,27,0.32);border:1px solid rgba(203,166,247,0.07);transition:border-color .15s,background .15s}
.file-item:hover{background:rgba(49,50,68,0.5)}
.file-item.active{border-color:var(--mauve);background:rgba(203,166,247,0.1)}
.file-meta{display:flex;flex-direction:column;min-width:0}
.file-name{font-family:var(--mono);font-size:0.88rem;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.file-lines{font-size:0.72rem;color:var(--overlay1)}
.star{flex:none;background:none;border:none;color:var(--overlay0);font-size:1.1rem;cursor:pointer;padding:4px 6px;border-radius:8px;transition:color .15s,background .15s}
.star:hover{color:var(--yellow);background:rgba(249,226,175,0.1)}
.file-item.active .star{color:var(--yellow)}

/* ---- editor ---- */
#editor-card{display:none}
#editor-card.show{display:block}
#editor-name{font-family:var(--mono);color:var(--mauve)}

/* ---- feed ---- */
.feed{max-height:320px;overflow-y:auto;display:flex;flex-direction:column}
.feed-item{display:flex;gap:12px;padding:9px 4px;border-bottom:1px solid rgba(203,166,247,0.06);font-family:var(--mono);font-size:0.8rem}
.feed-item:last-child{border-bottom:none}
.feed-time{color:var(--sapphire);flex:none}
.feed-text{color:var(--subtext1);word-break:break-word}
.empty{color:var(--overlay1);text-align:center;padding:26px 10px;font-size:0.86rem}

/* ---- toasts ---- */
#toasts{position:fixed;bottom:22px;right:22px;display:flex;flex-direction:column;gap:10px;z-index:60}
.toast{display:flex;align-items:center;gap:10px;padding:12px 16px;border-radius:13px;font-size:0.85rem;font-weight:500;
  background:var(--glass);backdrop-filter:blur(18px) saturate(160%);-webkit-backdrop-filter:blur(18px) saturate(160%);
  border:1px solid var(--glass-line);box-shadow:var(--shadow);color:var(--text);
  transform:translateX(120%);opacity:0;transition:transform .3s cubic-bezier(.2,.8,.2,1),opacity .3s;max-width:330px}
.toast.show{transform:translateX(0);opacity:1}
.toast-dot{width:9px;height:9px;border-radius:50%;flex:none}
.toast-success .toast-dot{background:var(--green);box-shadow:0 0 8px var(--green)}
.toast-error .toast-dot{background:var(--red);box-shadow:0 0 8px var(--red)}
.toast-info .toast-dot{background:var(--blue);box-shadow:0 0 8px var(--blue)}

/* ---- modal ---- */
#modal{position:fixed;inset:0;z-index:70;display:none;align-items:center;justify-content:center;padding:20px;
  background:rgba(17,17,27,0.6);backdrop-filter:blur(4px)}
#modal.open{display:flex}
.modal-card{width:100%;max-width:400px;padding:24px;background:var(--base);border:1px solid var(--glass-line);border-radius:var(--radius);box-shadow:0 24px 60px rgba(0,0,0,0.5)}
.modal-title{font-family:var(--display);font-size:1.15rem;font-weight:600;margin-bottom:8px}
.modal-msg{color:var(--subtext0);font-size:0.88rem;margin-bottom:16px}
.modal-input{margin-bottom:18px}
.modal-actions{display:flex;gap:11px;justify-content:flex-end}

::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--surface1);border-radius:99px}
::-webkit-scrollbar-thumb:hover{background:var(--surface2)}

@media (max-width:560px){
  header{flex-direction:column;align-items:stretch}
  .head-right{align-items:flex-start}
  .clock{font-size:1.8rem}
  .stats{grid-template-columns:repeat(2,1fr)}
}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="logo">🚀</div>
      <div>
        <h1>Dispatcher</h1>
        <p>message dispatcher</p>
      </div>
    </div>
    <div class="head-right">
      <div class="clock" id="clock">--:--:--</div>
      <div class="clock-date" id="clock-date">—</div>
      <div class="pill pill-off" id="conn"><span class="pill-text">connecting</span></div>
    </div>
  </header>

  <div class="grid">
    <!-- status + control -->
    <div class="card">
      <h2><span class="ic">📊</span> Status<span class="spacer"></span></h2>
      <div class="run-state idle" id="run-state">
        <span class="dot"></span>
        <span class="run-label">Idle</span>
        <span class="run-sub" id="updated"></span>
      </div>
      <div class="stats">
        <div class="stat"><div class="v" id="stat-sent">0</div><div class="l">Sent</div></div>
        <div class="stat"><div class="v" id="stat-total">0</div><div class="l">Total</div></div>
        <div class="stat"><div class="v" id="stat-progress">0%</div><div class="l">Progress</div></div>
        <div class="stat"><div class="v" id="stat-rate">—</div><div class="l">Msgs/min</div></div>
      </div>
      <div class="progress"><div class="fill" id="progress-fill"></div></div>
      <div class="error-box" id="error-box"><span class="err-ic">⚠</span><span class="err-text"></span></div>
      <div class="btn-row">
        <button class="btn btn-start" onclick="control('start')">▶ Start</button>
        <button class="btn btn-danger" onclick="control('stop')">⏹ Stop</button>
      </div>
    </div>

    <!-- configuration -->
    <div class="card">
      <h2><span class="ic">⚙️</span> Configuration</h2>
      <div class="field">
        <label>Channel ID</label>
        <input type="text" id="channel-id" placeholder="550390090665295892">
      </div>
      <div class="two">
        <div class="field"><label>Delay min (s)</label><input type="number" id="delay-min" value="6" min="1" step="0.5"></div>
        <div class="field"><label>Delay max (s)</label><input type="number" id="delay-max" value="7" min="1" step="0.5"></div>
      </div>
      <div class="toggles">
        <label class="switch"><input type="checkbox" id="loop-check"><span class="track"><span class="thumb"></span></span><span class="switch-label">Loop</span></label>
        <label class="switch"><input type="checkbox" id="feed-check"><span class="track"><span class="thumb"></span></span><span class="switch-label">Feed</span></label>
        <label class="switch"><input type="checkbox" id="autostart-check"><span class="track"><span class="thumb"></span></span><span class="switch-label">Auto-start</span></label>
      </div>
      <button class="btn btn-primary block" style="margin-top:14px" onclick="saveConfig()">Save settings</button>
    </div>
  </div>

  <div class="grid">
    <!-- files -->
    <div class="card">
      <h2><span class="ic">📁</span> Message files</h2>
      <div class="drop" id="drop" onclick="document.getElementById('file-input').click()">
        <b>Click to upload</b> or drop a .txt file
        <input type="file" id="file-input" accept=".txt" onchange="uploadFile(this)">
      </div>
      <div class="file-list" id="file-list"><p class="empty">Loading files…</p></div>
      <div class="btn-row"><button class="btn btn-ghost sm" onclick="createNewFile()">＋ New file</button></div>
    </div>

    <!-- editor -->
    <div class="card" id="editor-card">
      <h2><span class="ic">✏️</span> Editing&nbsp;<span id="editor-name">untitled.txt</span></h2>
      <textarea id="editor-content" placeholder="One message per line…"></textarea>
      <div class="btn-row">
        <button class="btn btn-start sm" onclick="saveFile()">Save</button>
        <button class="btn btn-primary sm" onclick="setActive()">Set active</button>
        <button class="btn btn-danger sm" onclick="deleteFile()">Delete</button>
        <button class="btn btn-ghost sm" onclick="closeEditor()">Close</button>
      </div>
    </div>

    <!-- feed -->
    <div class="card">
      <h2><span class="ic">📡</span> Live feed</h2>
      <div class="feed" id="feed"><p class="empty">Nothing sent yet. Hit Start to begin.</p></div>
    </div>
  </div>
</div>

<div id="toasts"></div>
<div id="modal"></div>

<script>
let currentFile=null, files=[], config={}, activeFile=null;

/* ---- clock ---- */
function tickClock(){
  const n=new Date();
  document.getElementById('clock').textContent=n.toLocaleTimeString('en-GB',{hour12:false});
  document.getElementById('clock-date').textContent=n.toLocaleDateString('en-GB',{weekday:'short',day:'2-digit',month:'short'});
}
setInterval(tickClock,1000); tickClock();

/* ---- helpers ---- */
function escapeAttr(s){return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;');}
function toast(msg,type='info'){
  const w=document.getElementById('toasts');
  const el=document.createElement('div');
  el.className='toast toast-'+type;
  el.innerHTML='<span class="toast-dot"></span><span class="toast-msg"></span>';
  el.querySelector('.toast-msg').textContent=msg;
  w.appendChild(el);
  requestAnimationFrame(()=>el.classList.add('show'));
  setTimeout(()=>{el.classList.remove('show');setTimeout(()=>el.remove(),320);},3200);
}
function modal(opts){
  return new Promise(resolve=>{
    const ov=document.getElementById('modal'); ov.innerHTML='';
    const card=document.createElement('div'); card.className='modal-card';
    card.innerHTML=
      '<h3 class="modal-title"></h3>'+
      (opts.message?'<p class="modal-msg"></p>':'')+
      (opts.input?'<input class="modal-input" type="text">':'')+
      '<div class="modal-actions"><button class="btn btn-ghost" data-act="cancel">Cancel</button>'+
      '<button class="btn '+(opts.danger?'btn-danger':'btn-primary')+'" data-act="ok"></button></div>';
    card.querySelector('.modal-title').textContent=opts.title||'';
    if(opts.message) card.querySelector('.modal-msg').textContent=opts.message;
    card.querySelector('[data-act=ok]').textContent=opts.okLabel||'Confirm';
    const inp=card.querySelector('.modal-input');
    if(inp){inp.placeholder=opts.placeholder||'';inp.value=opts.value||'';}
    ov.appendChild(card); ov.classList.add('open');
    if(inp){inp.focus();inp.select();}
    function close(r){ov.classList.remove('open');setTimeout(()=>ov.innerHTML='',200);resolve(r);}
    card.querySelector('[data-act=cancel]').onclick=()=>close(null);
    card.querySelector('[data-act=ok]').onclick=()=>close(inp?inp.value.trim():true);
    ov.onclick=e=>{if(e.target===ov)close(null);};
    if(inp)inp.onkeydown=e=>{if(e.key==='Enter')close(inp.value.trim());if(e.key==='Escape')close(null);};
  });
}

/* ---- polling ---- */
setInterval(fetchStatus,1000);
setInterval(fetchFiles,5000);
fetchStatus(); fetchFiles(); fetchConfig();

async function fetchStatus(){
  try{const r=await fetch('/api/status');updateStatus(await r.json());}
  catch(e){const p=document.getElementById('conn');p.className='pill pill-off';p.querySelector('.pill-text').textContent='disconnected';}
}
async function fetchConfig(){
  try{
    const r=await fetch('/api/config'); config=await r.json();
    document.getElementById('channel-id').value=config.channel_id||'';
    document.getElementById('delay-min').value=config.delay_min;
    document.getElementById('delay-max').value=config.delay_max;
    document.getElementById('loop-check').checked=!!config.loop;
    document.getElementById('feed-check').checked=!!config.feed;
    document.getElementById('autostart-check').checked=!!config.autostart;
    updateRate();
  }catch(e){}
}
async function fetchFiles(){
  try{const r=await fetch('/api/files');files=(await r.json()).files;renderFiles();}catch(e){}
}

function updateRate(){
  const a=(parseFloat(document.getElementById('delay-min').value)||6),
        b=(parseFloat(document.getElementById('delay-max').value)||7),
        avg=(a+b)/2;
  document.getElementById('stat-rate').textContent=avg>0?(60/avg).toFixed(1):'—';
}
['delay-min','delay-max'].forEach(id=>document.getElementById(id).addEventListener('input',updateRate));

function updateStatus(d){
  const p=document.getElementById('conn'); const on=!!d.connected;
  p.className='pill '+(on?'pill-on':'pill-off');
  p.querySelector('.pill-text').textContent=on?(d.bot_name||'connected'):'disconnected';

  const st=document.getElementById('run-state');
  if(d.running){st.className='run-state running';st.querySelector('.run-label').textContent='Sending';}
  else if(d.error){st.className='run-state error';st.querySelector('.run-label').textContent='Stopped';}
  else{st.className='run-state idle';st.querySelector('.run-label').textContent='Idle';}
  document.getElementById('updated').textContent=d.updated?('bot clock '+d.updated):'';

  const idx=d.index||0,tot=d.total||0,pct=tot?Math.round(idx/tot*100):0;
  document.getElementById('stat-sent').textContent=idx;
  document.getElementById('stat-total').textContent=tot;
  document.getElementById('stat-progress').textContent=pct+'%';
  document.getElementById('progress-fill').style.width=pct+'%';

  const eb=document.getElementById('error-box');
  if(d.error){eb.querySelector('.err-text').textContent=d.error;eb.classList.add('show');}
  else eb.classList.remove('show');

  const feed=document.getElementById('feed');
  if(d.feed&&d.feed.length){
    const arr=d.feed.slice().reverse();
    feed.innerHTML=arr.map(()=>'<div class="feed-item"><span class="feed-time"></span><span class="feed-text"></span></div>').join('');
    feed.querySelectorAll('.feed-item').forEach((el,i)=>{el.querySelector('.feed-time').textContent=arr[i].t;el.querySelector('.feed-text').textContent=arr[i].text;});
  }else feed.innerHTML='<p class="empty">Nothing sent yet. Hit Start to begin.</p>';

  if(d.active_file){activeFile=d.active_file;document.querySelectorAll('.file-item').forEach(el=>el.classList.toggle('active',el.dataset.name===activeFile));}
}

function renderFiles(){
  const list=document.getElementById('file-list');
  if(!files.length){list.innerHTML='<p class="empty">No files yet — upload a .txt or create one.</p>';return;}
  list.innerHTML=files.map(f=>
    '<div class="file-item'+(f.name===activeFile?' active':'')+'" data-name="'+escapeAttr(f.name)+'">'+
      '<div class="file-meta"><span class="file-name"></span><span class="file-lines">'+f.lines+' lines</span></div>'+
      '<button class="star" data-star="'+escapeAttr(f.name)+'" title="Set active">★</button>'+
    '</div>').join('');
  list.querySelectorAll('.file-item').forEach(el=>{el.querySelector('.file-name').textContent=el.dataset.name;});
}
document.getElementById('file-list').addEventListener('click',e=>{
  const star=e.target.closest('[data-star]');
  if(star){e.stopPropagation();quickSetActive(star.dataset.star);return;}
  const item=e.target.closest('.file-item');
  if(item)openFile(item.dataset.name);
});

async function openFile(name){
  try{
    const r=await fetch('/api/file/'+encodeURIComponent(name)); if(!r.ok)throw 0;
    const d=await r.json(); currentFile=d.name;
    document.getElementById('editor-name').textContent=d.name;
    document.getElementById('editor-content').value=d.content;
    const card=document.getElementById('editor-card'); card.classList.add('show');
    card.scrollIntoView({behavior:'smooth',block:'nearest'});
  }catch(e){toast('Could not open file','error');}
}
function closeEditor(){document.getElementById('editor-card').classList.remove('show');currentFile=null;}

async function saveFile(){
  if(!currentFile)return;
  try{
    await fetch('/api/file/'+encodeURIComponent(currentFile),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:document.getElementById('editor-content').value})});
    toast('Saved '+currentFile,'success'); fetchFiles();
  }catch(e){toast('Could not save file','error');}
}
async function deleteFile(){
  if(!currentFile)return;
  const ok=await modal({title:'Delete this file?',message:'"'+currentFile+'" will be removed. This can\'t be undone.',okLabel:'Delete',danger:true});
  if(!ok)return;
  try{await fetch('/api/file/'+encodeURIComponent(currentFile),{method:'DELETE'});toast('Deleted '+currentFile,'info');closeEditor();fetchFiles();}
  catch(e){toast('Could not delete file','error');}
}
function setActive(){if(currentFile)saveConfig(currentFile);}
function quickSetActive(name){saveConfig(name);}

async function createNewFile(){
  const name=await modal({title:'New message file',input:true,placeholder:'e.g. jokes',okLabel:'Create'});
  if(!name)return;
  const full=name.endsWith('.txt')?name:name+'.txt';
  try{
    await fetch('/api/file/'+encodeURIComponent(full),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:''})});
    toast('Created '+full,'success'); fetchFiles(); openFile(full);
  }catch(e){toast('Could not create file','error');}
}

async function uploadFile(input){
  const file=input.files[0]; if(!file)return;
  const form=new FormData(); form.append('file',file);
  try{await fetch('/api/upload',{method:'POST',body:form});input.value='';toast('Uploaded '+file.name,'success');fetchFiles();}
  catch(e){toast('Upload failed','error');}
}

async function saveConfig(activeFileName=null){
  const payload={
    channel_id:document.getElementById('channel-id').value,
    delay_min:parseFloat(document.getElementById('delay-min').value),
    delay_max:parseFloat(document.getElementById('delay-max').value),
    loop:document.getElementById('loop-check').checked,
    feed:document.getElementById('feed-check').checked,
    autostart:document.getElementById('autostart-check').checked,
  };
  if(activeFileName)payload.active_file=activeFileName;
  try{
    await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    toast(activeFileName?('Active file → '+activeFileName):'Settings saved','success');
    fetchConfig(); fetchFiles();
  }catch(e){toast('Could not save settings','error');}
}

async function control(action){
  try{
    const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});
    const d=await r.json();
    if(!d.ok)toast(d.error||'Action failed','error');
    else toast(action==='start'?'Sending started':'Sending stopped',action==='start'?'success':'info');
    fetchStatus();
  }catch(e){toast('Action failed','error');}
}

/* ---- drag & drop ---- */
const drop=document.getElementById('drop');
drop.addEventListener('dragover',e=>{e.preventDefault();drop.classList.add('over');});
drop.addEventListener('dragleave',()=>drop.classList.remove('over'));
drop.addEventListener('drop',e=>{
  e.preventDefault();drop.classList.remove('over');
  const file=e.dataTransfer.files[0];
  if(file&&file.name.endsWith('.txt')){const inp=document.getElementById('file-input');const dt=new DataTransfer();dt.items.add(file);inp.files=dt.files;uploadFile(inp);}
  else toast('Only .txt files are supported','error');
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
    return DASHBOARD_HTML


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

    cfg = read_config()

    # CHANNEL_ID env var pins the channel so it survives Render's disk wipes on
    # redeploy. When set (and numeric), it overrides the saved value on boot.
    env_channel = os.getenv("CHANNEL_ID", "").strip()
    if env_channel.isdigit():
        cfg["channel_id"] = int(env_channel)
        print(f"Channel ID pinned from CHANNEL_ID env: {env_channel}")

    # Auto-start: if enabled and prerequisites are set, begin posting on boot.
    # Re-applies on every process start (e.g. after a Render restart), so the
    # bot resumes by itself. A manual Stop during a session still sticks,
    # because this runs once at boot — not on reconnects.
    if cfg.get("autostart") and cfg.get("active_file") and cfg.get("channel_id"):
        cfg["running"] = True
        print("Auto-start enabled — posting begins once connected.")

    write_config(cfg)

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
