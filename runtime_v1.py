# ================== BOTFLOW V3 (CLEAN) ==================
import re, asyncio, aiohttp
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ----------------- UTIL -----------------

def inject(text: str, data: dict):
    def resolve(path):
        cur = data
        for k in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(k, "")
            else:
                return ""
        return cur
    return re.sub(r"\{(.*?)\}", lambda m: str(resolve(m.group(1))), text)

async def fetch_api(url):
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            try:
                return await r.json()
            except:
                return {"_raw": await r.text()}

# ----------------- AST NODES -----------------

@dataclass
class Node:
    type: str
    data: dict = field(default_factory=dict)
    children: List[Any] = field(default_factory=list)
    else_children: List[Any] = field(default_factory=list)

@dataclass
class Flow:
    name: str
    body: List[Node]

# ----------------- PARSER -----------------

class Parser:
    def __init__(self, script: str):
        self.lines = [l.rstrip() for l in script.splitlines() if l.strip() and not l.strip().startswith("#")]
        self.i = 0
        self.token = None
        self.flows: Dict[str, Flow] = {}

    def indent(self, line):
        return len(line) - len(line.lstrip())

    def parse(self):
        while self.i < len(self.lines):
            line = self.lines[self.i].strip()

            if line.startswith("token"):
                self.token = line.split(" ", 1)[1].strip('"')

            elif line.startswith("flow"):
                name = line.split()[1].replace(":", "")
                self.i += 1
                body = self.parse_block(self.indent(self.lines[self.i-1]))
                self.flows[name] = Flow(name, body)
                continue

            self.i += 1

        if not self.token:
            raise ValueError("Token missing")

        return self.token, self.flows

    def parse_block(self, base_indent):
        nodes = []

        while self.i < len(self.lines):
            raw = self.lines[self.i]
            indent = self.indent(raw)

            if indent <= base_indent:
                break

            line = raw.strip()

            # SAY
            if line.startswith("say"):
                node = self.parse_say(line)
                nodes.append(node)

            # ASK
            elif line.startswith("ask"):
                node = self.parse_ask(line, indent)
                nodes.append(node)
                continue

            # DO FETCH
            elif line.startswith("do fetch"):
                _, _, var, _, url = line.split(" ", 4)
                nodes.append(Node("fetch", {"var": var, "url": url.strip('"')}))

            # IF
            elif line.startswith("if"):
                node = self.parse_if(line, indent)
                nodes.append(node)
                continue

            # GO
            elif line.startswith("go"):
                nodes.append(Node("go", {"flow": line.split()[1]}))

            self.i += 1

        return nodes

    def parse_say(self, line):
        # say "text"
        if "photo" in line or "video" in line or "file" in line:
            parts = line.split()
            typ = parts[1]
            url = parts[2].strip('"')
            caption = ""
            if "caption" in line:
                caption = line.split("caption",1)[1].strip().strip('"')
            return Node("media", {"kind": typ, "url": url, "caption": caption})
        else:
            text = line[4:].strip().strip('"')
            return Node("say", {"text": text})

    def parse_ask(self, line, indent):
        parts = line.split()

        # button mode
        if line.endswith(":"):
            var = parts[1].replace(":", "")
            self.i += 1
            options = []

            while self.i < len(self.lines):
                l = self.lines[self.i]
                if self.indent(l) <= indent:
                    break
                label, val = l.strip().split("=>")
                options.append((label.strip().strip('"'), val.strip()))
                self.i += 1

            return Node("ask_buttons", {"var": var, "options": options})

        # text mode
        var = parts[1]
        text = line.split(" ",2)[2].strip('"')
        return Node("ask_input", {"var": var, "text": text})

    def parse_if(self, line, indent):
        parts = line.replace(":", "").split()
        key, op, val = parts[1], parts[2], parts[3].strip('"')

        self.i += 1
        children = self.parse_block(indent)

        else_children = []
        if self.i < len(self.lines) and self.lines[self.i].strip().startswith("else"):
            self.i += 1
            else_children = self.parse_block(indent)

        return Node("if", {"key": key, "op": op, "val": val}, children, else_children)

# ----------------- EXECUTOR -----------------

class Engine:
    def __init__(self, flows):
        self.flows = flows

    async def run(self, update, context):
        user = context.user_data

        while True:
            flow = user.get("flow", "start")
            pointer = user.get("ptr", 0)
            data = user.get("data", {})

            nodes = self.flows[flow].body

            if pointer >= len(nodes):
                return

            node = nodes[pointer]

            # SAY
            if node.type == "say":
                await update.effective_message.reply_text(inject(node.data["text"], data))
                user["ptr"] = pointer + 1

            # MEDIA
            elif node.type == "media":
                kind = node.data["kind"]
                fn = getattr(update.effective_message, f"reply_{kind}")
                await fn(
                    node.data["url"],
                    caption=inject(node.data["caption"], data)
                )
                user["ptr"] = pointer + 1

            # ASK INPUT
            elif node.type == "ask_input":
                user["wait"] = ("input", node.data["var"])
                user["ptr"] = pointer + 1
                await update.effective_message.reply_text(node.data["text"])
                return

            # ASK BUTTONS
            elif node.type == "ask_buttons":
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton(l, callback_data=f"sel:{node.data['var']}:{v}")]
                    for l,v in node.data["options"]
                ])
                user["wait"] = ("select", node.data["var"])
                user["ptr"] = pointer + 1
                await update.effective_message.reply_text("Choose:", reply_markup=kb)
                return

            # FETCH
            elif node.type == "fetch":
                data[node.data["var"]] = await fetch_api(node.data["url"])
                user["ptr"] = pointer + 1

            # IF
            elif node.type == "if":
                val = str(data.get(node.data["key"]))
                cond = False

                if node.data["op"] == "==":
                    cond = val == node.data["val"]
                elif node.data["op"] == "contains":
                    cond = node.data["val"] in val

                block = node.children if cond else node.else_children
                await self.run_block(block, update, context)
                user["ptr"] = pointer + 1

            # GO
            elif node.type == "go":
                user["flow"] = node.data["flow"]
                user["ptr"] = 0
                return

            user["data"] = data

    async def run_block(self, nodes, update, context):
        for node in nodes:
            # minimal inline execution (no pause here)
            if node.type == "say":
                await update.effective_message.reply_text(
                    inject(node.data["text"], context.user_data.get("data", {}))
                )
            elif node.type == "go":
                context.user_data["flow"] = node.data["flow"]
                context.user_data["ptr"] = 0
                return

# ----------------- TELEGRAM -----------------

def run(script: str):
    token, flows = Parser(script).parse()
    engine = Engine(flows)

    app = ApplicationBuilder().token(token).build()
    app.bot_data["engine"] = engine

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        context.user_data.update({"flow": "start", "ptr": 0, "data": {}})
        await engine.run(update, context)

    async def message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = context.user_data
        if "wait" in user:
            typ, var = user.pop("wait")
            user["data"][var] = update.message.text
        await engine.run(update, context)

    async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        user = context.user_data

        if q.data.startswith("sel:"):
            _, var, val = q.data.split(":")
            user["data"][var] = val

        await engine.run(q, context)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message))
    app.add_handler(CallbackQueryHandler(button))

    print("BotFlow v3 running...")
    app.run_polling()
