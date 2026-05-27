"""Telegram Bot — Claude Code remote control via MiMo API."""
import asyncio
import glob as globmod
import json
import logging
import os
import queue
import re
import subprocess
import tempfile
import threading
import traceback
from pathlib import Path

from PIL import ImageGrab

import anthropic
from telegram import InputFile, Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
)

# ── Logging ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")

# ── Config ──────────────────────────────────────────────────────
SETTINGS_FILE = Path(__file__).parent / "settings.json"
WORK_DIR = Path(__file__).parent
MODEL = "mimo-v2.5-pro"
MAX_TOKENS = 4096
MAX_HISTORY = 10
BASE_URL = "https://token-plan-cn.xiaomimimo.com/anthropic"
PROXY_URL = "http://127.0.0.1:10090"

os.environ.setdefault("HTTP_PROXY", PROXY_URL)
os.environ.setdefault("HTTPS_PROXY", PROXY_URL)


def _load_setting(key: str) -> str | None:
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f).get(key)
    except (FileNotFoundError, json.JSONDecodeError):
        return os.environ.get(key.upper())


TELEGRAM_TOKEN = _load_setting("telegram_bot_token")
ANTHROPIC_API_KEY = _load_setting("anthropic_api_key") or _load_setting("api_key")

# ── Conversation state ──────────────────────────────────────────
_histories: dict[int, list] = {}
_locks: dict[int, threading.Lock] = {}


def _get_history(chat_id: int) -> list:
    if chat_id not in _histories:
        _histories[chat_id] = []
    return _histories[chat_id]


def _get_lock(chat_id: int) -> threading.Lock:
    if chat_id not in _locks:
        _locks[chat_id] = threading.Lock()
    return _locks[chat_id]


# ── Tool definitions ────────────────────────────────────────────
TOOLS = [
    {
        "name": "read_file",
        "description": "Read file contents. Returns line-numbered text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative or absolute)"},
                "offset": {"type": "integer", "description": "Start line (0-based)"},
                "limit": {"type": "integer", "description": "Max lines to read"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file, creating directories if needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace old_string with new_string in a file (exact match).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "bash",
        "description": "Execute a shell command and return stdout+stderr. Use this to open programs, run scripts, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 15)"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "glob_files",
        "description": "Find files matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"},
                "path": {"type": "string", "description": "Root directory (default: current)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "grep",
        "description": "Search for a regex pattern in files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern"},
                "path": {"type": "string", "description": "File or directory to search"},
                "glob": {"type": "string", "description": "File glob filter, e.g. '*.py'"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "list_dir",
        "description": "List files and directories at a given path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (default: current)"},
            },
            "required": [],
        },
    },
    {
        "name": "screenshot",
        "description": "Take a screenshot of the computer screen and send it to the user.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]

SYSTEM_PROMPT = (
    "You are a helpful assistant running on the user's Windows computer. "
    "You can read, write, edit files, run shell commands, and search the filesystem. "
    "The working directory is: {work_dir}\n\n"
    "Rules:\n"
    "- Always use bash tool to open programs (e.g. `start notepad`, `start chrome`).\n"
    "- Keep responses SHORT and concise — they display on a phone screen.\n"
    "- After executing a tool, briefly confirm what you did.\n"
    "- Use Chinese to respond.\n"
    "- NEVER modify, edit, or delete the telegram_bot.py file.\n"
    "- NEVER kill python processes or the bot process.\n"
    "- NEVER run taskkill, shutdown, restart, or format commands."
)


# ── Tool execution ──────────────────────────────────────────────
def _resolve(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = WORK_DIR / p
    return p.resolve()


def _exec_read_file(args: dict) -> str:
    p = _resolve(args["path"])
    if not p.exists():
        return f"Error: file not found: {p}"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    offset = args.get("offset", 0)
    limit = args.get("limit", 2000)
    chunk = lines[offset:offset + limit]
    return "\n".join(f"{i + offset + 1}\t{line}" for i, line in enumerate(chunk))


def _exec_write_file(args: dict) -> str:
    p = _resolve(args["path"])
    if "telegram_bot.py" in str(p):
        return "Error: cannot modify telegram_bot.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args["content"], encoding="utf-8")
    return f"Wrote {len(args['content'])} chars to {p}"


def _exec_edit_file(args: dict) -> str:
    p = _resolve(args["path"])
    if "telegram_bot.py" in str(p):
        return "Error: cannot modify telegram_bot.py"
    if not p.exists():
        return f"Error: file not found: {p}"
    text = p.read_text(encoding="utf-8")
    old, new = args["old_string"], args["new_string"]
    if old not in text:
        return f"Error: old_string not found in {p}"
    count = text.count(old)
    text = text.replace(old, new, 1)
    p.write_text(text, encoding="utf-8")
    remaining = count - 1
    return f"Replaced 1 occurrence in {p}" + (f" ({remaining} more remain)" if remaining else "")


def _exec_bash(args: dict) -> str:
    cmd = args["command"]
    timeout = args.get("timeout", 15)
    cmd_lower = cmd.lower().strip()
    for blocked in ["taskkill", "shutdown", "restart", "format"]:
        if blocked in cmd_lower:
            return f"Error: command blocked for safety"
    log.info(f"Executing bash: {cmd}")
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=str(WORK_DIR),
        )
        output = result.stdout
        if result.stderr:
            output += ("\n" if output else "") + result.stderr
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return output[:30000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


def _exec_glob(args: dict) -> str:
    pattern = args["pattern"]
    root = args.get("path", str(WORK_DIR))
    matches = globmod.glob(os.path.join(root, pattern), recursive=True)
    if not matches:
        return "(no matches)"
    return "\n".join(sorted(matches)[:500])


def _exec_grep(args: dict) -> str:
    pattern = args["pattern"]
    path = args.get("path", str(WORK_DIR))
    glob_filter = args.get("glob")
    p = Path(path)
    if p.is_file():
        files = [p]
    else:
        if glob_filter:
            files = list(p.rglob(glob_filter))[:200]
        else:
            files = list(p.rglob("*"))[:200]
        files = [f for f in files if f.is_file()]
    results = []
    regex = re.compile(pattern)
    for f in files:
        try:
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if regex.search(line):
                    results.append(f"{f}:{i}: {line[:200]}")
                    if len(results) >= 200:
                        return "\n".join(results) + "\n(truncated)"
        except Exception:
            pass
    return "\n".join(results) if results else "(no matches)"


def _exec_list_dir(args: dict) -> str:
    p = _resolve(args.get("path", "."))
    if not p.is_dir():
        return f"Error: not a directory: {p}"
    entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    lines = []
    for e in entries[:200]:
        prefix = "[dir] " if e.is_dir() else "      "
        lines.append(f"{prefix}{e.name}")
    return "\n".join(lines) if lines else "(empty)"


def _exec_screenshot(args: dict) -> str:
    log.info("Taking screenshot...")
    try:
        img = ImageGrab.grab()
        path = os.path.join(tempfile.gettempdir(), "screenshot.png")
        img.save(path, "PNG")
        log.info(f"Screenshot saved: {path}")
        return f"SCREENSHOT:{path}"
    except Exception as e:
        return f"Error taking screenshot: {e}"




TOOL_EXECUTORS = {
    "read_file": _exec_read_file,
    "write_file": _exec_write_file,
    "edit_file": _exec_edit_file,
    "bash": _exec_bash,
    "glob_files": _exec_glob,
    "grep": _exec_grep,
    "list_dir": _exec_list_dir,
    "screenshot": _exec_screenshot,
}


def execute_tool(name: str, args: dict) -> str:
    fn = TOOL_EXECUTORS.get(name)
    if not fn:
        return f"Unknown tool: {name}"
    try:
        return fn(args)
    except Exception:
        return f"Error: {traceback.format_exc()}"


# ── Claude API call with progress queue ─────────────────────────
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("api_key not set in settings.json")
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, base_url=BASE_URL)
    return _client


def chat_with_claude(chat_id: int, user_message: str, progress_q: queue.Queue):
    """Call MiMo API with tool use. Puts progress updates into progress_q."""
    lock = _get_lock(chat_id)
    lock.acquire()
    try:
        _chat_inner(chat_id, user_message, progress_q)
    finally:
        lock.release()


def _chat_inner(chat_id: int, user_message: str, progress_q: queue.Queue):
    history = _get_history(chat_id)
    history.append({"role": "user", "content": user_message})

    while len(history) > MAX_HISTORY:
        history.pop(0)
    # Ensure history starts with a user message (not orphaned tool_result)
    while history and history[0].get("role") == "user" and isinstance(history[0].get("content"), list):
        history.pop(0)

    system = SYSTEM_PROMPT.format(work_dir=WORK_DIR)
    client = _get_client()

    for round_num in range(10):
        log.info(f"API call round {round_num + 1}, history={len(history)} msgs")
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=TOOLS,
                messages=history,
            )
        except Exception as e:
            log.error(f"API error: {e}")
            progress_q.put(("error", str(e)))
            return

        assistant_content = resp.content
        history.append({"role": "assistant", "content": assistant_content})
        log.info(f"API response: stop_reason={resp.stop_reason}")

        if resp.stop_reason == "end_turn":
            texts = [b.text for b in assistant_content if b.type == "text"]
            final = "\n".join(texts) or "(empty response)"
            progress_q.put(("text", final))
            return

        # Execute tools
        tool_results = []
        for block in assistant_content:
            if block.type == "tool_use":
                preview = json.dumps(block.input, ensure_ascii=False)[:100]
                log.info(f"Tool: {block.name}({preview})")
                progress_q.put(("tool", block.name, preview))
                result = execute_tool(block.name, block.input)
                log.info(f"Tool result: {result[:200]}")

                # Handle screenshot: send image to user, tell API it's done
                if result.startswith("SCREENSHOT:"):
                    img_path = result.split(":", 1)[1]
                    progress_q.put(("image", img_path))
                    api_result = "Screenshot taken and sent to user."
                else:
                    api_result = result[:20000]

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": api_result,
                })

        if not tool_results:
            texts = [b.text for b in assistant_content if b.type == "text"]
            final = "\n".join(texts) or "(no text)"
            progress_q.put(("text", final))
            return

        history.append({"role": "user", "content": tool_results})

    progress_q.put(("text", "(达到最大工具调用轮数)"))


# ── Telegram handlers ───────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Claude Code Bot 已连接!\n\n"
        "直接发消息即可对话。支持的命令:\n"
        "/clear — 清除对话历史\n"
        "/cd <path> — 切换工作目录\n"
        "/status — 查看状态"
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    _histories.pop(chat_id, None)
    await update.message.reply_text("对话历史已清除。")


async def cmd_cd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global WORK_DIR
    if not context.args:
        await update.message.reply_text(f"当前目录: {WORK_DIR}")
        return
    new_dir = Path(" ".join(context.args))
    if not new_dir.is_absolute():
        new_dir = WORK_DIR / new_dir
    new_dir = new_dir.resolve()
    if new_dir.is_dir():
        WORK_DIR = new_dir
        await update.message.reply_text(f"工作目录: {WORK_DIR}")
    else:
        await update.message.reply_text(f"目录不存在: {new_dir}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    hist = _get_history(chat_id)
    await update.message.reply_text(
        f"Model: {MODEL}\n"
        f"Work dir: {WORK_DIR}\n"
        f"History: {len(hist)} msgs\n"
        f"API: {'OK' if ANTHROPIC_API_KEY else 'NOT SET'}"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text
    if not text:
        return

    log.info(f"[{chat_id}] User: {text[:100]}")

    # Send initial "thinking" message
    status_msg = await update.message.reply_text("思考中...")

    progress_q: queue.Queue = queue.Queue()

    # Run API call in thread
    thread = threading.Thread(
        target=chat_with_claude,
        args=(chat_id, text, progress_q),
        daemon=True,
    )
    thread.start()

    # Process progress updates
    final_text = None
    while thread.is_alive() or not progress_q.empty():
        try:
            item = progress_q.get(timeout=0.5)
        except queue.Empty:
            # Keep typing indicator active
            try:
                await update.message.chat.send_action("typing")
            except Exception:
                pass
            continue

        if item[0] == "tool":
            _, tool_name, preview = item
            tool_display = {
                "bash": "执行命令",
                "read_file": "读取文件",
                "write_file": "写入文件",
                "edit_file": "编辑文件",
                "glob_files": "搜索文件",
                "grep": "搜索内容",
                "list_dir": "列出目录",
                "screenshot": "截图",
            }.get(tool_name, tool_name)
            try:
                await status_msg.edit_text(f"{tool_display}: {preview[:60]}")
            except Exception:
                pass

        elif item[0] == "image":
            _, img_path = item
            try:
                await status_msg.edit_text("发送截图...")
            except Exception:
                pass
            try:
                with open(img_path, "rb") as f:
                    await update.message.reply_photo(photo=InputFile(f))
            except Exception as e:
                log.error(f"Failed to send image: {e}")
                await update.message.reply_text(f"截图失败: {e}")
            finally:
                try:
                    if os.path.exists(img_path):
                        os.remove(img_path)
                        log.info(f"Screenshot deleted: {img_path}")
                except Exception as e:
                    log.error(f"Failed to delete screenshot: {e}")

        elif item[0] == "text":
            final_text = item[1]

        elif item[0] == "error":
            final_text = f"错误: {item[1]}"

    # Delete status message and send final response
    try:
        await status_msg.delete()
    except Exception:
        pass

    if final_text:
        log.info(f"[{chat_id}] Reply: {final_text[:200]}")
        for i in range(0, len(final_text), 4000):
            chunk = final_text[i:i + 4000]
            await update.message.reply_text(chunk, parse_mode=None)
    else:
        await update.message.reply_text("(无响应)")


# ── Main ────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        print("ERROR: telegram_bot_token not set in settings.json")
        return
    if not ANTHROPIC_API_KEY:
        print("ERROR: api_key not set in settings.json")
        return

    log.info(f"Starting bot — Model: {MODEL}, Proxy: {PROXY_URL}")
    log.info(f"API key: ...{ANTHROPIC_API_KEY[-4:]}")

    app = Application.builder().token(TELEGRAM_TOKEN).proxy(PROXY_URL).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Polling started. Send /start in Telegram.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
