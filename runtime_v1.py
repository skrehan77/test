import re
import aiohttp
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ---------------- PARSER ----------------

def parse(script):
    lines = [l.rstrip() for l in script.splitlines() if l.strip()]
    flows = {}
    token = None
    current = None
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        if line.startswith("token"):
            token = line.split(" ", 1)[1]

        elif line.startswith("flow"):
            current = line.split(" ", 1)[1]
            flows[current] = []

        elif current:
            # SEND
            if line.startswith("send"):
                flows[current].append({"type": "send", "text": line[5:]})

            # INPUT
            elif line.startswith("input"):
                _, var, *msg = line.split(" ")
                flows[current].append({
                    "type": "input",
                    "var": var,
                    "text": " ".join(msg)
                })

            # SELECT
            elif line.startswith("select"):
                var = line.split()[1]
                options = []
                i += 1
                while i < len(lines) and "=" in lines[i]:
                    l, v = lines[i].split("=")
                    options.append((l.strip(), v.strip()))
                    i += 1
                i -= 1
                flows[current].append({
                    "type": "select",
                    "var": var,
                    "options": options
                })

            # FETCH
            elif line.startswith("fetch"):
                _, var, url = line.split(" ", 2)
                flows[current].append({
                    "type": "fetch",
                    "var": var,
                    "url": url
                })

            # IF
            elif line.startswith("if"):
                _, key, _, val = line.split(" ")
                flows[current].append({
                    "type": "if",
                    "key": key,
                    "value": val
                })

            # GO
            elif line.startswith("go"):
                flows[current].append({
                    "type": "go",
                    "flow": line.split()[1]
                })

        i += 1

    return token, flows


# ---------------- UTILS ----------------

def inject(text, data):
    return re.sub(r"\{(.*?)\}", lambda m: str(data.get(m.group(1), "")), text)


async def fetch_api(url):
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            try:
                return await r.json()
            except:
                return {"error": "invalid_json"}


# ---------------- ENGINE ----------------

async def execute(update, context):
    user = context.user_data
    flows = context.bot_data["flows"]

    flow = user.get("flow", "start")
    pointer = user.get("pointer", 0)
    data = user.get("data", {})

    nodes = flows.get(flow, [])

    while pointer < len(nodes):
        node = nodes[pointer]

        # ---- SEND ----
        if node["type"] == "send":
            await update.effective_message.reply_text(
                inject(node["text"], data),
                parse_mode=ParseMode.MARKDOWN
            )

        # ---- INPUT ----
        elif node["type"] == "input":
            user.update({
                "wait": ("input", node["var"]),
                "flow": flow,
                "pointer": pointer + 1,
                "data": data
            })
            await update.effective_message.reply_text(node["text"])
            return

        # ---- SELECT ----
        elif node["type"] == "select":
            buttons = [
                [InlineKeyboardButton(lbl, callback_data=f"sel:{node['var']}:{val}")]
                for lbl, val in node["options"]
            ]

            user.update({
                "wait": ("select", node["var"]),
                "flow": flow,
                "pointer": pointer + 1,
                "data": data
            })

            await update.effective_message.reply_text(
                f"Choose {node['var']}",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            return

        # ---- FETCH ----
        elif node["type"] == "fetch":
            url = inject(node["url"], data)
            data[node["var"]] = await fetch_api(url)

        # ---- IF ----
        elif node["type"] == "if":
            val = data.get(node["key"])
            if str(val) != node["value"]:
                pointer += 1
                continue

        # ---- GO ----
        elif node["type"] == "go":
            user.update({
                "flow": node["flow"],
                "pointer": 0
            })
            return await execute(update, context)

        pointer += 1

    user.update({
        "flow": flow,
        "pointer": pointer,
        "data": data
    })


# ---------------- HANDLERS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data.update({
        "flow": "start",
        "pointer": 0,
        "data": {}
    })
    await execute(update, context)


async def message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = context.user_data

    if "wait" in user:
        typ, var = user.pop("wait")
        user["data"][var] = update.message.text

    await execute(update, context)


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user = context.user_data

    if q.data.startswith("sel:"):
        _, var, val = q.data.split(":")
        user["data"][var] = val

    await execute(q, context)


# ---------------- RUN ----------------

def run(script):
    token, flows = parse(script)

    app = ApplicationBuilder().token(token).build()
    app.bot_data["flows"] = flows

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message))
    app.add_handler(CallbackQueryHandler(button))

    print("BotFlow running...")
    app.run_polling()
