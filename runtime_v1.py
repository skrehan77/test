import re
import asyncio
import aiohttp
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)


# ─────────────────────────────────────────────
# PARSER
# ─────────────────────────────────────────────

def parse(script: str):
    """Parse the DSL script into a token and a dict of flows."""
    token = None
    flows = {}
    current_flow = None

    lines = script.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # skip blank lines and comments
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        indent = len(line) - len(line.lstrip())

        # top-level directives (no indent)
        if indent == 0:
            if stripped.startswith("token "):
                token = stripped[6:].strip()
            elif stripped.startswith("flow "):
                current_flow = stripped[5:].strip()
                flows[current_flow] = []

        # flow body (indented)
        elif current_flow is not None:
            node, i = _parse_node(lines, i, indent)
            if node:
                flows[current_flow].append(node)
            continue

        i += 1

    return token, flows


def _parse_node(lines, i, base_indent):
    """Parse a single node from the current line, consuming extra lines if needed."""
    line = lines[i]
    stripped = line.strip()
    i += 1

    # SEND
    if stripped.startswith("send "):
        return {"type": "send", "text": stripped[5:]}, i

    # IMAGE
    if stripped.startswith("image "):
        return {"type": "image", "url": stripped[6:].strip()}, i

    # INPUT
    if stripped.startswith("input "):
        parts = stripped[6:].split(" ", 1)
        var = parts[0]
        prompt = parts[1] if len(parts) > 1 else f"Enter {var}:"
        return {"type": "input", "var": var, "text": prompt}, i

    # SET
    if stripped.startswith("set "):
        rest = stripped[4:]
        var, _, val = rest.partition("=")
        return {"type": "set", "var": var.strip(), "value": val.strip()}, i

    # FETCH
    if stripped.startswith("fetch "):
        parts = stripped[6:].split(" ", 1)
        return {"type": "fetch", "var": parts[0], "url": parts[1].strip()}, i

    # GO
    if stripped.startswith("go "):
        return {"type": "go", "flow": stripped[3:].strip()}, i

    # DELAY
    if stripped.startswith("delay "):
        return {"type": "delay", "seconds": float(stripped[6:].strip())}, i

    # SELECT (multi-line: options follow as indented "Label = value")
    if stripped.startswith("select "):
        var = stripped[7:].strip()
        options = []
        while i < len(lines):
            next_line = lines[i]
            if not next_line.strip() or next_line.strip().startswith("#"):
                i += 1
                continue
            next_indent = len(next_line) - len(next_line.lstrip())
            if next_indent <= base_indent:
                break
            if "=" in next_line:
                label, _, value = next_line.strip().partition("=")
                options.append((label.strip(), value.strip()))
                i += 1
            else:
                break
        return {"type": "select", "var": var, "options": options}, i

    # BUTTONS (URL buttons: Label = https://...)
    if stripped.startswith("buttons "):
        prompt = stripped[8:].strip()
        buttons = []
        while i < len(lines):
            next_line = lines[i]
            if not next_line.strip() or next_line.strip().startswith("#"):
                i += 1
                continue
            next_indent = len(next_line) - len(next_line.lstrip())
            if next_indent <= base_indent:
                break
            if "=" in next_line:
                label, _, url = next_line.strip().partition("=")
                buttons.append((label.strip(), url.strip()))
                i += 1
            else:
                break
        return {"type": "buttons", "text": prompt, "buttons": buttons}, i

    # IF (single or multi-line body)
    if stripped.startswith("if "):
        condition = stripped[3:].strip()
        body = []
        while i < len(lines):
            next_line = lines[i]
            if not next_line.strip() or next_line.strip().startswith("#"):
                i += 1
                continue
            next_indent = len(next_line) - len(next_line.lstrip())
            if next_indent <= base_indent:
                break
            node, i = _parse_node(lines, i, next_indent)
            if node:
                body.append(node)
        return {"type": "if", "condition": condition, "body": body}, i

    return None, i


# ─────────────────────────────────────────────
# UTILS
# ─────────────────────────────────────────────

def resolve(text: str, data: dict) -> str:
    """Replace {var} and {var.key.subkey} with values from data."""
    def replacer(m):
        parts = m.group(1).split(".")
        val = data
        for p in parts:
            if isinstance(val, dict):
                val = val.get(p, "")
            elif isinstance(val, list):
                try:
                    val = val[int(p)]
                except (ValueError, IndexError):
                    val = ""
            else:
                val = ""
        return str(val)
    return re.sub(r"\{([\w.]+)\}", replacer, text)


def eval_condition(condition: str, data: dict) -> bool:
    """
    Evaluate a simple condition string.
    Supports:  var == value
               var != value
               var contains value
               var  (truthy check)
    """
    condition = condition.strip()

    if " contains " in condition:
        left, _, right = condition.partition(" contains ")
        return right.strip() in str(data.get(left.strip(), ""))

    if " != " in condition:
        left, _, right = condition.partition(" != ")
        return str(data.get(left.strip(), "")) != right.strip()

    if " == " in condition:
        left, _, right = condition.partition(" == ")
        return str(data.get(left.strip(), "")) == right.strip()

    # truthy check
    val = data.get(condition, "")
    return bool(val)


async def fetch_url(url: str):
    """Fetch a URL and return parsed JSON or raw text."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "json" in content_type:
                try:
                    return await resp.json()
                except Exception:
                    pass
            return await resp.text()


# ─────────────────────────────────────────────
# ENGINE
# ─────────────────────────────────────────────

async def run_nodes(nodes, update, context, data):
    """
    Execute a list of nodes, modifying data in-place.
    Returns a dict with:
      - "wait": True if execution paused for user input
      - "go": flow name if a jump was requested
    """
    msg = update.effective_message

    for idx, node in enumerate(nodes):
        t = node["type"]

        if t == "send":
            await msg.reply_text(resolve(node["text"], data), parse_mode=ParseMode.MARKDOWN)

        elif t == "image":
            url = resolve(node["url"], data)
            await msg.reply_photo(photo=url)

        elif t == "set":
            data[node["var"]] = resolve(node["value"], data)

        elif t == "delay":
            await asyncio.sleep(node["seconds"])

        elif t == "fetch":
            url = resolve(node["url"], data)
            data[node["var"]] = await fetch_url(url)

        elif t == "input":
            context.user_data.update({
                "_waiting_for": node["var"],
                "_resume_nodes": nodes[idx + 1:],
                "_data": data,
            })
            await msg.reply_text(resolve(node["text"], data))
            return {"wait": True}

        elif t == "select":
            kb = [
                [InlineKeyboardButton(label, callback_data=f"__sel__{node['var']}__{val}")]
                for label, val in node["options"]
            ]
            context.user_data.update({
                "_waiting_for": f"__select__{node['var']}",
                "_resume_nodes": nodes[idx + 1:],
                "_data": data,
            })
            await msg.reply_text(
                f"Choose {node['var']}:",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return {"wait": True}

        elif t == "buttons":
            kb = [
                [InlineKeyboardButton(label, url=url)]
                for label, url in node["buttons"]
            ]
            await msg.reply_text(
                resolve(node["text"], data),
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif t == "if":
            if eval_condition(node["condition"], data):
                result = await run_nodes(node["body"], update, context, data)
                if result:
                    return result  # propagate waits/jumps

        elif t == "go":
            return {"go": node["flow"]}

    return {}


async def execute(update, context):
    """Main execution loop: run the current flow and follow jumps."""
    user = context.user_data
    flows = context.bot_data["flows"]

    # Resume from a mid-flow pause (input/select)
    resume_nodes = user.pop("_resume_nodes", None)
    data = user.pop("_data", user.get("data", {}))
    flow = user.get("flow", "start")

    if resume_nodes is not None:
        nodes = resume_nodes
    else:
        nodes = flows.get(flow, [])
        data = user.get("data", {})

    # Follow jumps (go) up to a safety limit
    for _ in range(50):
        result = await run_nodes(nodes, update, context, data)

        if result.get("wait"):
            user["data"] = data
            return

        target = result.get("go")
        if target:
            flow = target
            user["flow"] = flow
            nodes = flows.get(flow, [])
            continue

        # flow finished normally
        user["data"] = data
        user["flow"] = flow
        return

    # Safety: loop limit hit
    await update.effective_message.reply_text("⚠️ Flow loop limit reached.")


# ─────────────────────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["flow"] = "start"
    await execute(update, context)


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow users to restart the bot at any time with /restart."""
    await cmd_start(update, context)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = context.user_data
    waiting_for = user.pop("_waiting_for", None)

    if waiting_for and not waiting_for.startswith("__select__"):
        # store the user's text in data
        data = user.get("_data", user.get("data", {}))
        data[waiting_for] = update.message.text
        user["_data"] = data

    await execute(update, context)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = context.user_data

    if query.data.startswith("__sel__"):
        # format: __sel__<var>__<value>
        _, _, rest = query.data.partition("__sel__")
        var, _, val = rest.partition("__")
        data = user.get("_data", user.get("data", {}))
        data[var] = val
        user["_data"] = data
        user.pop("_waiting_for", None)

    await execute(query, context)


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def run(script: str):
    """Parse the DSL script and start the Telegram bot."""
    token, flows = parse(script)

    if not token:
        raise ValueError("No token found in script. Add: token YOUR_BOT_TOKEN")
    if not flows:
        raise ValueError("No flows found in script. Add at least: flow start")

    app = ApplicationBuilder().token(token).build()
    app.bot_data["flows"] = flows

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_button))

    print(f"BotFlow v2 running — {len(flows)} flow(s) loaded: {', '.join(flows)}")
    app.run_polling()
