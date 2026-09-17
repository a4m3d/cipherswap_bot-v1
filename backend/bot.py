"""Telegram bot: auto-bridge USDC (Base) -> STRK (Starknet) via NEAR Intents.

Privacy-first: a fresh deposit address is generated for every swap, we store
the minimum needed, and users can wipe all their data with /forget.
"""
import asyncio
import io
import logging
import random
import re
import secrets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import qrcode
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from near_client import NearBridgeClient, TERMINAL_STATUSES

logger = logging.getLogger("bridge_bot")

# Conversation states
BR_RECIPIENT, BR_RECIPIENT_TEXT, BR_REFUND, BR_REFUND_TEXT, BR_AMOUNT, BR_BLEND, BR_PRIVACY = range(7)

CROWD_AMOUNTS = [Decimal(x) for x in (5, 10, 25, 50, 100, 250, 500, 1000)]
SPLIT_MIN = Decimal("2")

STARKNET_RE = re.compile(r"^0x[0-9a-fA-F]{1,64}$")
EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
MIN_USDC = Decimal("1")

_poll_task = None


# ---------- helpers ----------
def _db(context):
    return context.application.bot_data["db"]


def _near(context) -> NearBridgeClient:
    return context.application.bot_data["near"]


async def _get_user(db, chat_id: int) -> dict:
    user = await db.users.find_one({"_id": chat_id})
    return user or {"_id": chat_id, "starknet": [], "base": []}


async def _save_address(db, chat_id: int, kind: str, address: str):
    user = await _get_user(db, chat_id)
    lst = user.get(kind, [])
    if address not in lst:
        lst.append(address)
        await db.users.update_one({"_id": chat_id}, {"$set": {kind: lst}}, upsert=True)


def _short(addr: str) -> str:
    return f"{addr[:8]}...{addr[-6:]}" if addr and len(addr) > 16 else addr


def _friendly_err(e: Exception) -> str:
    s = str(e).lower()
    if "timeout" in s or "timed out" in s or not str(e).strip():
        return "the bridge is busy right now — please try again in a moment"
    if "min" in s and "amount" in s:
        return "that amount is below the bridge minimum — try a bit more"
    return str(e)[:180]


def _qr_bytes(text: str) -> io.BytesIO:
    img = qrcode.make(text)
    bio = io.BytesIO()
    bio.name = "deposit.png"
    img.save(bio, "PNG")
    bio.seek(0)
    return bio


BASE_USDC_CONTRACT = "0x833589FCd6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_CHAIN_ID = 8453


def _payment_link(deposit_address: str, amount: Decimal) -> str:
    """EIP-681 style URI so mobile wallets pre-fill token, network and amount."""
    raw = int((amount * (10 ** 6)).to_integral_value())
    return (
        f"ethereum:pay-{BASE_USDC_CONTRACT}@{BASE_CHAIN_ID}/transfer"
        f"?address={deposit_address}&uint256={raw}"
    )


async def _send_deposit_card(bot, chat_id, qr_text, caption, sid):
    """Send the QR deposit/invoice card with a Cancel button; fall back to text."""
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel transaction", callback_data=f"cxl:{sid}")]])
    try:
        await bot.send_photo(
            chat_id, photo=InputFile(_qr_bytes(qr_text)),
            caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
        )
    except Exception:
        logger.exception("deposit card photo failed; sending text fallback")
        try:
            await bot.send_message(chat_id, caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            logger.exception("deposit card text fallback also failed")


async def cb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    sid = q.data.split(":", 1)[1]
    db = _db(context)
    swap = await db.swaps.find_one({"sid": sid, "chat_id": q.message.chat.id})
    if not swap:
        await _ack(q, "This transaction was not found.", show_alert=True)
        return
    if swap.get("status") == "PENDING_DEPOSIT":
        await db.swaps.update_one({"_id": swap["_id"]}, {"$set": {"status": "CANCELLED"}})
        await _ack(q, "Transaction cancelled.")
        try:
            await q.edit_message_caption(
                caption="✖️ *Transaction cancelled.*\nDo not send any funds to that address.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
    else:
        await _ack(q, "Too late to cancel — a deposit was already detected on-chain.", show_alert=True)


# ---------- basic commands ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    near = _near(context)
    text = (
        "🛰️ *Base → Starknet Auto-Bridge*\n\n"
        f"Send *{near.origin_symbol}* on *Base*, receive *{near.dest_symbol}* on *Starknet* "
        "at your address — automatically.\n\n"
        "Tap /bridge to start. Each bridge uses a *brand-new deposit address*, and you can turn on "
        "Blend-In amounts, address rotation, Split and Zero-Trace right in the flow.\n\n"
        "Commands:\n"
        "• /bridge — start a bridge (with privacy options)\n"
        "• /addresses — manage saved addresses\n"
        "• /history — recent bridges\n"
        "• /privacy — how we protect you\n"
        "• /forget — wipe all your saved data"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start a bridge", callback_data="go:bridge")]])
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🔒 *Privacy toolkit* (choose these inside /bridge)\n\n"
        "• *Fresh address every time* — always on; your wallets are never reused or linked.\n"
        "• *🫥 Blend-In Amounts* — round to common amounts (25/50/100) to hide in the crowd.\n"
        "• *🎲 Rotate receiving wallet* — spread income across several Starknet addresses.\n"
        "• *🔀 Split* — break a transfer into random chunks, each with its own address.\n"
        "• *⏱ Delays* — space chunks over time to defeat amount+time correlation.\n"
        "• *🕵️ Zero-Trace* — keep no history; records self-destruct after completion.\n\n"
        "Use /forget any time to erase everything.\n\n"
        "⚠️ Honest note: no bridge is *100%* untraceable, but stacking these makes tracking extremely hard."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = _db(context)
    chat_id = update.effective_chat.id
    await db.users.delete_one({"_id": chat_id})
    await db.swaps.delete_many({"chat_id": chat_id})
    await update.effective_message.reply_text(
        "🧹 Done. All your saved addresses and bridge history have been erased."
    )


async def cmd_addresses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = _db(context)
    user = await _get_user(db, update.effective_chat.id)
    sn, base = user.get("starknet", []), user.get("base", [])
    if not sn and not base:
        await update.effective_message.reply_text(
            "You have no saved addresses yet. Start a /bridge and I'll offer to save them."
        )
        return
    lines = ["*📇 Saved addresses*\n"]
    buttons = []
    if sn:
        lines.append("*Starknet (receive):*")
        for i, a in enumerate(sn):
            lines.append(f"  • `{a}`")
            buttons.append([InlineKeyboardButton(f"🗑 Delete Starknet {_short(a)}", callback_data=f"del:starknet:{i}")])
    if base:
        lines.append("\n*Base (refund):*")
        for i, a in enumerate(base):
            lines.append(f"  • `{a}`")
            buttons.append([InlineKeyboardButton(f"🗑 Delete Base {_short(a)}", callback_data=f"del:base:{i}")])
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


async def cb_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _ack(q)
    _, kind, idx = q.data.split(":")
    db = _db(context)
    user = await _get_user(db, q.message.chat.id)
    lst = user.get(kind, [])
    idx = int(idx)
    if 0 <= idx < len(lst):
        removed = lst.pop(idx)
        await db.users.update_one({"_id": q.message.chat.id}, {"$set": {kind: lst}}, upsert=True)
        await q.edit_message_text(f"🗑 Removed `{_short(removed)}`.", parse_mode=ParseMode.MARKDOWN)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = _db(context)
    cur = db.swaps.find({"chat_id": update.effective_chat.id}).sort("created_at", -1).limit(10)
    swaps = await cur.to_list(10)
    if not swaps:
        await update.effective_message.reply_text("No bridges yet. Tap /bridge to make your first one.")
        return
    emoji = {"SUCCESS": "✅", "REFUNDED": "↩️", "FAILED": "❌", "PROCESSING": "⏳"}
    lines = ["*🧾 Recent bridges*\n"]
    for s in swaps:
        st = s.get("status", "PENDING_DEPOSIT")
        lines.append(
            f"{emoji.get(st, '⏳')} {s.get('amount_in','?')} USDC → "
            f"~{s.get('amount_out','?')} STRK · *{st}*\n   to `{_short(s.get('recipient',''))}`"
        )
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ---------- bridge conversation ----------
async def bridge_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    db = _db(context)
    chat_id = update.effective_chat.id
    user = await _get_user(db, chat_id)
    sn = user.get("starknet", [])
    if sn:
        buttons = [[InlineKeyboardButton(f"📥 {_short(a)}", callback_data=f"sn:{i}")] for i, a in enumerate(sn)]
        if len(sn) >= 2:
            buttons.append([InlineKeyboardButton("🎲 Auto-rotate across my addresses", callback_data="sn:rot")])
        buttons.append([InlineKeyboardButton("➕ Enter a new address", callback_data="sn:new")])
        try:
            await update.effective_message.reply_text(
                "🌉 *New bridge*\n\nWhich *Starknet* address should receive the STRK?",
                parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons),
            )
        except Exception:
            logger.exception("bridge_start reply failed")
        return BR_RECIPIENT
    try:
        await update.effective_message.reply_text(
            "🌉 *New bridge*\n\nSend me your *Starknet* address (0x...) to receive the funds.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        logger.exception("bridge_start reply failed")
    return BR_RECIPIENT_TEXT


async def _safe_edit(q, text):
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass


async def _ack(q, text=None, show_alert=False):
    try:
        if text:
            await q.answer(text, show_alert=show_alert)
        else:
            await q.answer()
    except Exception:
        pass


async def _safe_send(bot, chat_id, text, **kwargs):
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except Exception:
        logger.exception("prompt send failed")
        return None


async def _safe_reply(update, text, **kwargs):
    try:
        return await update.effective_message.reply_text(text, **kwargs)
    except Exception:
        logger.exception("reply failed")
        return None


async def cb_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _ack(q)
    if q.data == "sn:new":
        await _safe_edit(q, "Send me your *Starknet* address (0x...).")
        return BR_RECIPIENT_TEXT
    if q.data == "sn:rot":
        context.user_data["rotate"] = True
        await _safe_edit(q, "🎲 *Auto-rotate on* — each bridge lands on a different saved Starknet address.")
        return await _ask_refund(update, context)
    idx = int(q.data.split(":")[1])
    user = await _get_user(_db(context), q.message.chat.id)
    context.user_data["recipient"] = user["starknet"][idx]
    await _safe_edit(q, f"Receiving to `{_short(context.user_data['recipient'])}` ✅")
    return await _ask_refund(update, context)


async def recipient_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = update.message.text.strip()
    if not STARKNET_RE.match(addr):
        await _safe_reply(update, "⚠️ That doesn't look like a Starknet address (0x + hex). Try again.")
        return BR_RECIPIENT_TEXT
    context.user_data["recipient"] = addr
    await _save_address(_db(context), update.effective_chat.id, "starknet", addr)
    await _safe_reply(update, "Saved Starknet address ✅")
    return await _ask_refund(update, context)


async def _ask_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = _db(context)
    user = await _get_user(db, update.effective_chat.id)
    base = user.get("base", [])
    chat = update.effective_chat
    if base:
        buttons = [[InlineKeyboardButton(f"↩️ {_short(a)}", callback_data=f"bs:{i}")] for i, a in enumerate(base)]
        buttons.append([InlineKeyboardButton("➕ Enter a new address", callback_data="bs:new")])
        await _safe_send(
            context.bot, chat.id,
            "Which *Base* address are you sending *from*? (used for refunds if anything fails)",
            parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons),
        )
        return BR_REFUND
    await _safe_send(
        context.bot, chat.id,
        "Now send your *Base* address (the wallet you'll send USDC *from* — used for refunds). 0x + 40 hex.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return BR_REFUND_TEXT


async def cb_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _ack(q)
    if q.data == "bs:new":
        await _safe_edit(q, "Send me your *Base* refund address (0x + 40 hex).")
        return BR_REFUND_TEXT
    idx = int(q.data.split(":")[1])
    user = await _get_user(_db(context), q.message.chat.id)
    context.user_data["refund"] = user["base"][idx]
    await _safe_edit(q, f"Refunds to `{_short(context.user_data['refund'])}` ✅")
    return await _ask_amount(update, context)


async def refund_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = update.message.text.strip()
    if not EVM_RE.match(addr):
        await _safe_reply(update, "⚠️ That's not a valid Base (EVM) address. Send 0x + 40 hex characters.")
        return BR_REFUND_TEXT
    context.user_data["refund"] = addr
    await _save_address(_db(context), update.effective_chat.id, "base", addr)
    await _safe_reply(update, "Saved Base address ✅")
    return await _ask_amount(update, context)


async def _ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _safe_send(
        context.bot, update.effective_chat.id,
        f"💵 How much *USDC* do you want to bridge? (minimum {MIN_USDC})",
        parse_mode=ParseMode.MARKDOWN,
    )
    return BR_AMOUNT


async def amount_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().replace(",", "")
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError):
        await _safe_reply(update, "⚠️ Please send a number, e.g. 25")
        return BR_AMOUNT
    if amount < MIN_USDC:
        await _safe_reply(update, f"⚠️ Minimum is {MIN_USDC} USDC.")
        return BR_AMOUNT
    context.user_data["amount"] = amount

    suggestions = _blend_suggestions(amount)
    if suggestions:
        buttons = [[InlineKeyboardButton(f"🫥 Bridge {a} USDC (blend in)", callback_data=f"bl:{a}")] for a in suggestions]
        buttons.append([InlineKeyboardButton(f"Use my exact {amount} USDC", callback_data="bl:keep")])
        await _safe_reply(
            update,
            "🫥 *Blend-In Amounts*\nRound amounts hide among thousands of identical transfers — "
            "much harder to trace than an odd number.\n\n"
            f"Round *{amount}* to a common amount?",
            parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons),
        )
        return BR_BLEND
    return await _show_privacy(update, context)


def _blend_suggestions(amount: Decimal):
    if amount in CROWD_AMOUNTS:
        return []
    res = []
    higher = [c for c in CROWD_AMOUNTS if c >= amount]
    if higher:
        res.append(higher[0])
    nearest = min(CROWD_AMOUNTS, key=lambda c: abs(c - amount))
    if nearest not in res:
        res.append(nearest)
    return res[:2]


async def cb_blend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _ack(q)
    data = q.data.split(":", 1)[1]
    if data != "keep":
        context.user_data["amount"] = Decimal(data)
    await _safe_edit(q, f"Amount set: *{context.user_data['amount']} USDC* ✅")
    return await _show_privacy(update, context)


def _privacy_keyboard(prv: dict) -> InlineKeyboardMarkup:
    split_label = {0: "Off", 1: "2–3 chunks", 2: "3–4 chunks"}[prv["split"]]
    delay_label = {0: "Off", 1: "≤5 min", 2: "≤30 min"}[prv["delay"]]
    zt_label = "On" if prv["zt"] else "Off"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔀 Split: {split_label}", callback_data="prv:split")],
        [InlineKeyboardButton(f"⏱ Delays: {delay_label}", callback_data="prv:delay")],
        [InlineKeyboardButton(f"🕵️ Zero-Trace: {zt_label}", callback_data="prv:zt")],
        [InlineKeyboardButton("✅ Confirm & get address", callback_data="prv:go")],
    ])


async def _show_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prv = context.user_data.setdefault("prv", {"split": 0, "delay": 0, "zt": False})
    rot = "on 🎲" if context.user_data.get("rotate") else "off"
    text = (
        "🛡️ *Privacy options* — tap to toggle:\n\n"
        "• *Split* — break the amount into random chunks, each with its own fresh address\n"
        "• *Delays* — space the chunks out over time (needs Split; strongest anti-tracking)\n"
        "• *Zero-Trace* — keep no history; the record self-destructs once it completes\n"
        f"• *Rotate receiving wallet*: {rot}\n\n"
        "Then tap *Confirm*."
    )
    await _safe_send(
        context.bot, update.effective_chat.id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=_privacy_keyboard(prv)
    )
    return BR_PRIVACY


async def cb_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    prv = context.user_data.setdefault("prv", {"split": 0, "delay": 0, "zt": False})
    action = q.data.split(":", 1)[1]
    if action == "split":
        prv["split"] = (prv["split"] + 1) % 3
    elif action == "delay":
        prv["delay"] = (prv["delay"] + 1) % 3
    elif action == "zt":
        prv["zt"] = not prv["zt"]
    elif action == "go":
        await _ack(q, "Setting up your deposit...")
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return await _execute_plan(update, context)
    await _ack(q)
    try:
        await q.edit_message_reply_markup(reply_markup=_privacy_keyboard(prv))
    except Exception:
        pass
    return BR_PRIVACY


async def _resolve_recipient(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str:
    ud = context.user_data
    if not ud.get("rotate"):
        return ud["recipient"]
    db = _db(context)
    user = await _get_user(db, chat_id)
    lst = user.get("starknet", [])
    if not lst:
        return ud.get("recipient")
    idx = int(user.get("rot_idx", 0)) % len(lst)
    await db.users.update_one({"_id": chat_id}, {"$set": {"rot_idx": idx + 1}}, upsert=True)
    return lst[idx]


def _split_amount(total: Decimal, n: int):
    total = Decimal(total)
    max_n = int(total / SPLIT_MIN)
    n = max(1, min(n, max_n))
    if n <= 1:
        return [total.quantize(Decimal("0.01"))]
    weights = [random.random() + 0.2 for _ in range(n)]
    s = sum(weights)
    chunks = [(total * Decimal(str(w / s))).quantize(Decimal("0.01")) for w in weights]
    for i in range(len(chunks)):
        if chunks[i] < SPLIT_MIN:
            chunks[i] = SPLIT_MIN
    diff = (total - sum(chunks)).quantize(Decimal("0.01"))
    chunks[-1] = (chunks[-1] + diff).quantize(Decimal("0.01"))
    while len(chunks) > 1 and chunks[-1] < SPLIT_MIN:
        merged = chunks.pop()
        chunks[-1] = (chunks[-1] + merged).quantize(Decimal("0.01"))
    return chunks


async def _create_and_send(bot, db, chat_id, amount, recipient, refund, near,
                           gid=None, ephemeral=False, label=None):
    try:
        quote = await near.create_swap(amount, recipient, refund)
    except Exception as e:
        logger.exception("chunk quote failed")
        await _safe_send(bot, chat_id, f"❌ Couldn't create a deposit for {amount} USDC: {_friendly_err(e)}")
        return
    deposit = quote["deposit_address"]
    link = _payment_link(deposit, amount)
    sid = secrets.token_hex(4)
    await db.swaps.insert_one({
        "sid": sid,
        "gid": gid,
        "chat_id": chat_id,
        "deposit_address": deposit,
        "deposit_memo": quote.get("deposit_memo"),
        "recipient": recipient,
        "refund": refund,
        "amount_in": str(amount),
        "amount_out": quote.get("amount_out_formatted"),
        "status": "PENDING_DEPOSIT",
        "ephemeral": ephemeral,
        "correlation_id": quote.get("correlation_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    memo_line = f"\n*Memo:* `{quote['deposit_memo']}`" if quote.get("deposit_memo") else ""
    lbl = f" — chunk {label[0]} of {label[1]}" if label else ""
    zt = "\n🕵️ Zero-Trace: this record self-destructs after completion." if ephemeral else ""
    caption = (
        f"✅ *Deposit address ready*{lbl}\n\n"
        f"Send exactly *{amount} USDC* on *Base* to:\n`{deposit}`{memo_line}\n\n"
        f"You'll receive *~{quote.get('amount_out_formatted','?')} STRK* "
        f"(~${quote.get('amount_out_usd','?')}) on Starknet\n"
        f"→ `{_short(recipient)}`\n"
        f"⏱ ETA ~{quote.get('time_estimate','?')}s after deposit confirms\n"
        f"🔒 Single-use address. Scan to auto-fill the amount.{zt}"
    )
    await _send_deposit_card(bot, chat_id, link, caption, sid)
    await _safe_send(bot, chat_id, f"👇 Tap to copy the address:\n`{deposit}`", parse_mode=ParseMode.MARKDOWN)


async def _split_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    d = job.data
    app = context.application
    if d["gid"] in app.bot_data.setdefault("cancelled_gids", set()):
        return
    db = app.bot_data["db"]
    near = app.bot_data["near"]
    await _safe_send(context.bot, job.chat_id, f"⏱ Time to send chunk {d['idx']} of {d['total']} — {d['amount']} USDC:")
    await _create_and_send(
        context.bot, db, job.chat_id, Decimal(d["amount"]), d["recipient"], d["refund"], near,
        gid=d["gid"], ephemeral=d["ephemeral"], label=(d["idx"], d["total"]),
    )


async def _execute_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ud = context.user_data
    amount = ud["amount"]
    refund = ud["refund"]
    prv = ud.get("prv", {"split": 0, "delay": 0, "zt": False})
    ephemeral = bool(prv.get("zt"))
    db = _db(context)
    near = _near(context)
    n = {0: 1, 1: random.randint(2, 3), 2: random.randint(3, 4)}[prv.get("split", 0)]

    if n == 1:
        rec = await _resolve_recipient(context, chat_id)
        await _create_and_send(context.bot, db, chat_id, amount, rec, refund, near, ephemeral=ephemeral)
        ud.clear()
        return ConversationHandler.END

    chunks = _split_amount(amount, n)
    recipients = [await _resolve_recipient(context, chat_id) for _ in chunks]
    gid = secrets.token_hex(4)
    delay_max = {0: 0, 1: 300, 2: 1800}[prv.get("delay", 0)]

    summary = (
        f"🔀 *Split plan* — {len(chunks)} chunks totalling {amount} USDC:\n"
        + "\n".join([f"  • Chunk {i+1}: {c} USDC" for i, c in enumerate(chunks)])
        + "\n\nEach uses a *fresh address*"
        + (" — I'll ping you when to send the next one." if delay_max else " — send them in any order.")
    )
    await _safe_send(
        context.bot, chat_id, summary, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel remaining plan", callback_data=f"cxlp:{gid}")]]),
    )

    await _create_and_send(context.bot, db, chat_id, chunks[0], recipients[0], refund, near,
                           gid=gid, ephemeral=ephemeral, label=(1, len(chunks)))
    cum = 0
    for i, (c, rec) in enumerate(zip(chunks[1:], recipients[1:]), start=2):
        if delay_max:
            cum += random.randint(30, delay_max)
            context.job_queue.run_once(
                _split_job, when=cum, chat_id=chat_id, name=gid,
                data={"amount": str(c), "recipient": rec, "refund": refund,
                      "ephemeral": ephemeral, "gid": gid, "idx": i, "total": len(chunks)},
            )
        else:
            await _create_and_send(context.bot, db, chat_id, c, rec, refund, near,
                                   gid=gid, ephemeral=ephemeral, label=(i, len(chunks)))
    ud.clear()
    return ConversationHandler.END


async def cb_cancel_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    gid = q.data.split(":", 1)[1]
    db = _db(context)
    context.application.bot_data.setdefault("cancelled_gids", set()).add(gid)
    await db.swaps.update_many({"gid": gid, "status": "PENDING_DEPOSIT"}, {"$set": {"status": "CANCELLED"}})
    try:
        for j in context.job_queue.get_jobs_by_name(gid):
            j.schedule_removal()
    except Exception:
        pass
    await _ack(q, "Remaining plan cancelled.")
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await context.bot.send_message(
        q.message.chat.id,
        "✖️ Remaining chunks cancelled. Any chunk you already sent will still complete normally.",
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text("Cancelled. Tap /bridge whenever you're ready.")
    return ConversationHandler.END


async def cb_go_bridge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry from the inline 'Start a bridge' button."""
    await _ack(update.callback_query)
    return await bridge_start(update, context)


# ---------- background status poller ----------
async def _poller(app: Application):
    db = app.bot_data["db"]
    near = app.bot_data["near"]
    labels = {
        "KNOWN_DEPOSIT_TX": "📥 Deposit detected — bridging now...",
        "PENDING_DEPOSIT": None,
        "INCOMPLETE_DEPOSIT": "⚠️ Partial deposit received. Send the remaining amount.",
        "PROCESSING": "⏳ Bridging your funds...",
        "SUCCESS": "✅ Done! Your STRK has arrived on Starknet.",
        "REFUNDED": "↩️ The swap was refunded to your Base address.",
        "FAILED": "❌ The bridge failed. If you deposited, funds are refunded to your Base address.",
    }
    while True:
        try:
            cur = db.swaps.find({"status": {"$nin": list(TERMINAL_STATUSES) + ["CANCELLED"]}})
            active = await cur.to_list(200)
            for s in active:
                try:
                    res = await near.get_status(s["deposit_address"], s.get("deposit_memo"))
                except Exception:
                    continue
                new_status = res["status"]
                if new_status != s.get("status"):
                    await db.swaps.update_one(
                        {"_id": s["_id"]}, {"$set": {"status": new_status}}
                    )
                    msg = labels.get(new_status)
                    if msg:
                        try:
                            await app.bot.send_message(s["chat_id"], msg)
                        except Exception:
                            logger.warning("could not notify chat %s", s["chat_id"])
                    if s.get("ephemeral") and new_status in TERMINAL_STATUSES:
                        await db.swaps.delete_one({"_id": s["_id"]})
        except Exception:
            logger.exception("poller loop error")
        await asyncio.sleep(15)


def start_poller(app: Application):
    global _poll_task
    _poll_task = asyncio.create_task(_poller(app))


async def stop_poller():
    global _poll_task
    if _poll_task:
        _poll_task.cancel()
        _poll_task = None


# ---------- application factory ----------
async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("handler error", exc_info=context.error)
    try:
        chat_id = None
        if isinstance(update, Update) and update.effective_chat:
            chat_id = update.effective_chat.id
        if chat_id:
            await context.bot.send_message(
                chat_id,
                "⚠️ Something went wrong on my side. Please try again, or send /cancel and restart.",
            )
    except Exception:
        pass


async def conv_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    try:
        if update and update.effective_chat:
            await context.bot.send_message(
                update.effective_chat.id,
                "⌛ Session timed out. Tap /bridge to start again.",
            )
    except Exception:
        pass
    return ConversationHandler.END


def create_application(token: str, db, near: NearBridgeClient) -> Application:
    app = Application.builder().token(token).updater(None).build()
    app.bot_data["db"] = db
    app.bot_data["near"] = near
    app.add_error_handler(_on_error)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("bridge", bridge_start),
            CallbackQueryHandler(cb_go_bridge, pattern="^go:bridge$"),
        ],
        states={
            BR_RECIPIENT: [CallbackQueryHandler(cb_recipient, pattern="^sn:")],
            BR_RECIPIENT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, recipient_text)],
            BR_REFUND: [CallbackQueryHandler(cb_refund, pattern="^bs:")],
            BR_REFUND_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, refund_text)],
            BR_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, amount_text)],
            BR_BLEND: [CallbackQueryHandler(cb_blend, pattern="^bl:")],
            BR_PRIVACY: [CallbackQueryHandler(cb_privacy, pattern="^prv:")],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, conv_timeout),
                CallbackQueryHandler(conv_timeout),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
        conversation_timeout=300,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("addresses", cmd_addresses))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CallbackQueryHandler(cb_cancel, pattern="^cxl:"))
    app.add_handler(CallbackQueryHandler(cb_cancel_plan, pattern="^cxlp:"))
    app.add_handler(CallbackQueryHandler(cb_delete, pattern="^del:"))
    return app
