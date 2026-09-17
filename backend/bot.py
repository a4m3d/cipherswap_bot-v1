"""Telegram bot: private cross-chain swaps/bridges via NEAR Intents.

Two modes:
  • Classic  — Base USDC -> Starknet STRK (the original privacy flow)
  • Universal — any coin, any of 35 networks, by menu or natural language
Privacy suite: fresh addresses, blend-in amounts, address rotation, split
(multi-address non-custodial OR pay-once custodial), delays, zero-trace.
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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

import evm
import nlp
from near_client import NearBridgeClient, TERMINAL_STATUSES, net_name, BASE_USDC_ASSET, STRK_ASSET

logger = logging.getLogger("bridge_bot")

# Universal conversation states
(US_SRC_NET, US_SRC_COIN, US_DST_NET, US_DST_COIN, US_AMOUNT,
 US_RECIPIENT, US_RECIPIENT_TEXT, US_REFUND, US_REFUND_TEXT, US_PRIVACY) = range(10)

CROWD_AMOUNTS = [Decimal(x) for x in (5, 10, 25, 50, 100, 250, 500, 1000)]
SPLIT_MIN = Decimal("2")
MIN_USD = Decimal("1")
_poll_task = None
_custodial_task = None


# ---------------- small helpers ----------------
def _db(context):
    return context.application.bot_data["db"]


def _near(context) -> NearBridgeClient:
    return context.application.bot_data["near"]


async def _get_user(db, chat_id):
    u = await db.users.find_one({"_id": chat_id})
    return u or {"_id": chat_id, "book": {}, "rot_idx": 0}


async def _save_addr(db, chat_id, network, address):
    await db.users.update_one({"_id": chat_id}, {"$addToSet": {f"book.{network}": address}}, upsert=True)


def _book(user, network):
    return (user.get("book") or {}).get(network, [])


def _short(a):
    return f"{a[:8]}…{a[-6:]}" if a and len(a) > 16 else a


def _qr_bytes(text):
    bio = io.BytesIO(); bio.name = "q.png"
    qrcode.make(text).save(bio, "PNG"); bio.seek(0)
    return bio


def _friendly_err(e):
    s = str(e).lower()
    if "timeout" in s or "timed out" in s or not str(e).strip():
        return "the network is busy right now — please try again in a moment"
    if "min" in s and "amount" in s:
        return "that amount is below the route minimum — try a bit more"
    return str(e)[:180]


async def _ack(q, text=None, show_alert=False):
    try:
        await q.answer(text, show_alert=show_alert) if text else await q.answer()
    except Exception:
        pass


async def _safe_edit(q, text):
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass


async def _safe_send(bot, chat_id, text, **kw):
    try:
        return await bot.send_message(chat_id, text, **kw)
    except Exception:
        logger.exception("send failed")
        return None


async def _safe_reply(update, text, **kw):
    try:
        return await update.effective_message.reply_text(text, **kw)
    except Exception:
        logger.exception("reply failed")
        return None


def _payment_link(token, network, deposit, amount, decimals):
    """EIP-681 for EVM chains so wallets pre-fill amount; else the plain address."""
    if evm.supported(network) and token:
        raw = int((Decimal(str(amount)) * (Decimal(10) ** decimals)).to_integral_value())
        cid = evm.EVM_NETWORKS[network]["chain_id"]
        return f"ethereum:pay-{token}@{cid}/transfer?address={deposit}&uint256={raw}"
    return deposit


# ---------------- start / help / privacy / clear ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🛰️ *CipherSwap* — private cross-chain swaps\n\n"
        "Pick a mode, or just *type* what you want, e.g.\n"
        "`swap 5 USDC on base to USDT on bsc`\n"
        "`bridge 20 ETH on arb to SOL`\n\n"
        "🔒 Fresh address every time · optional split, delays & zero-trace."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌉 Classic — Base USDC → Starknet", callback_data="mode:classic")],
        [InlineKeyboardButton("🌀 Universal Swap — any coin / chain", callback_data="mode:uni")],
        [InlineKeyboardButton("📇 Address book", callback_data="show:book"),
         InlineKeyboardButton("🧹 Clear history", callback_data="clr:ask")],
    ])
    await _safe_reply(update, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def cmd_help(update, context):
    await cmd_start(update, context)


async def cmd_privacy(update, context):
    await _safe_reply(update,
        "🔒 *Privacy toolkit* (toggle these before confirming a swap)\n\n"
        "• *Fresh address* every swap — always on\n"
        "• *🫥 Blend-In* — round to crowd amounts (25/50/100…)\n"
        "• *🎲 Rotate* — spread across your saved destination addresses\n"
        "• *🔀 Split* — random chunks, each its own address\n"
        "   – *Multi-address* (non-custodial) or *Pay-once* (custodial, EVM)\n"
        "• *⏱ Delays* — space chunks over time (Monero-style timing defence)\n"
        "• *🕵️ Zero-Trace* — no history; record self-destructs\n\n"
        "Use /clear to wipe everything. No bridge is 100% untraceable, but stacking these makes tracing extremely hard.",
        parse_mode=ParseMode.MARKDOWN)


async def cmd_clear(update, context):
    await _clear_prompt(update.effective_chat.id, context)


async def _clear_prompt(chat_id, context):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Yes, wipe everything", callback_data="clr:yes")],
        [InlineKeyboardButton("Cancel", callback_data="clr:no")],
    ])
    await _safe_send(context.bot, chat_id,
        "🧹 *Clear history?*\nThis deletes your saved addresses, swap history and any pending custodial jobs from the bot.\n"
        "_Note: Telegram doesn't let bots erase the visible chat — long-press messages to delete your side._",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def cb_clear(update, context):
    q = update.callback_query
    action = q.data.split(":")[1]
    if action == "ask":
        await _ack(q)
        await _clear_prompt(q.message.chat.id, context)
        return
    if action == "no":
        await _ack(q, "Kept.")
        await _safe_edit(q, "Kept your data.")
        return
    db = _db(context); chat_id = q.message.chat.id
    await db.users.delete_one({"_id": chat_id})
    await db.swaps.delete_many({"chat_id": chat_id})
    await db.custodial.delete_many({"chat_id": chat_id})
    await _ack(q, "Wiped.")
    await _safe_edit(q, "🧹 Done — all your saved data and history were erased from the bot.")


async def cmd_forget(update, context):
    db = _db(context); chat_id = update.effective_chat.id
    await db.users.delete_one({"_id": chat_id})
    await db.swaps.delete_many({"chat_id": chat_id})
    await db.custodial.delete_many({"chat_id": chat_id})
    await _safe_reply(update, "🧹 All your saved data and history erased.")


async def cmd_addresses(update, context):
    await _show_book(update.effective_chat.id, context)


async def cb_show_book(update, context):
    await _ack(update.callback_query)
    await _show_book(update.callback_query.message.chat.id, context)


async def _show_book(chat_id, context):
    user = await _get_user(_db(context), chat_id)
    book = user.get("book") or {}
    if not book:
        await _safe_send(context.bot, chat_id, "Your address book is empty. It fills up as you swap.")
        return
    lines = ["*📇 Address book*\n"]
    buttons = []
    for net, addrs in book.items():
        lines.append(f"*{net_name(net)}:*")
        for i, a in enumerate(addrs):
            lines.append(f"  • `{a}`")
            buttons.append([InlineKeyboardButton(f"🗑 {net_name(net)} {_short(a)}", callback_data=f"del:{net}:{i}")])
    await _safe_send(context.bot, chat_id, "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
                     reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def cb_delete(update, context):
    q = update.callback_query
    _, net, idx = q.data.split(":")
    db = _db(context); user = await _get_user(db, q.message.chat.id)
    addrs = _book(user, net)
    idx = int(idx)
    if 0 <= idx < len(addrs):
        removed = addrs.pop(idx)
        await db.users.update_one({"_id": q.message.chat.id}, {"$set": {f"book.{net}": addrs}})
        await _ack(q, "Removed.")
        await _safe_edit(q, f"🗑 Removed `{_short(removed)}`.")


async def cmd_history(update, context):
    db = _db(context)
    swaps = await db.swaps.find({"chat_id": update.effective_chat.id}).sort("created_at", -1).limit(10).to_list(10)
    if not swaps:
        await _safe_reply(update, "No swaps yet. Type e.g. `swap 5 USDC on base to USDT on bsc` or /start.")
        return
    emoji = {"SUCCESS": "✅", "REFUNDED": "↩️", "FAILED": "❌", "PROCESSING": "⏳", "CANCELLED": "✖️"}
    lines = ["*🧾 Recent swaps*\n"]
    for s in swaps:
        st = s.get("status", "PENDING_DEPOSIT")
        lines.append(f"{emoji.get(st,'⏳')} {s.get('amount_in','?')} {s.get('src_sym','?')} "
                     f"({net_name(s.get('src_net',''))}) → {s.get('dst_sym','?')} ({net_name(s.get('dst_net',''))}) · *{st}*")
    await _safe_reply(update, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ---------------- mode entry ----------------
async def cb_mode(update, context):
    q = update.callback_query
    await _ack(q)
    context.user_data.clear()
    mode = q.data.split(":")[1]
    if mode == "classic":
        context.user_data["route"] = {
            "origin_asset": BASE_USDC_ASSET, "origin_decimals": 6, "origin_net": "base",
            "src_sym": "USDC", "dest_asset": STRK_ASSET, "dest_net": "starknet",
            "dst_sym": "STRK", "origin_contract": "0x833589FCd6eDb6E08f4c7C32D4f71b54bdA02913",
        }
        await _safe_edit(q, "🌉 *Classic:* Base USDC → Starknet STRK", )
        return await _ask_amount(update, context)
    await _safe_edit(q, "🌀 *Universal Swap* — choose the source network:")
    return await _ask_src_net(update, context)


async def cmd_swap(update, context):
    context.user_data.clear()
    return await _ask_src_net(update, context)


# ---------------- universal guided menus ----------------
def _net_grid(prefix):
    cat = _CATALOG
    nets = cat.networks()
    rows, row = [], []
    for n in nets:
        row.append(InlineKeyboardButton(net_name(n), callback_data=f"{prefix}:{n}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


_CATALOG = None  # set in create_application


async def _ask_src_net(update, context):
    await _safe_send(context.bot, update.effective_chat.id, "1️⃣ *Source network* — where your coins are now:",
                     parse_mode=ParseMode.MARKDOWN, reply_markup=_net_grid("usn"))
    return US_SRC_NET


async def cb_src_net(update, context):
    q = update.callback_query; await _ack(q)
    net = q.data.split(":")[1]
    context.user_data.setdefault("route", {})["origin_net"] = net
    coins = _CATALOG.coins_on(net)
    rows, row = [], []
    for t in coins:
        row.append(InlineKeyboardButton(t["symbol"], callback_data=f"usc:{t['symbol']}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    await _safe_edit(q, f"Source: *{net_name(net)}*")
    await _safe_send(context.bot, q.message.chat.id, "2️⃣ *Which coin* are you sending?",
                     parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))
    return US_SRC_COIN


async def cb_src_coin(update, context):
    q = update.callback_query; await _ack(q)
    sym = q.data.split(":")[1]
    r = context.user_data["route"]
    t = _CATALOG.find(sym, r["origin_net"])
    r.update({"src_sym": sym, "origin_asset": t["assetId"], "origin_decimals": t["decimals"],
              "origin_contract": t.get("contractAddress")})
    await _safe_edit(q, f"Sending: *{sym}* on {net_name(r['origin_net'])}")
    await _safe_send(context.bot, q.message.chat.id, "3️⃣ *Destination network*:",
                     parse_mode=ParseMode.MARKDOWN, reply_markup=_net_grid("udn"))
    return US_DST_NET


async def cb_dst_net(update, context):
    q = update.callback_query; await _ack(q)
    net = q.data.split(":")[1]
    context.user_data["route"]["dest_net"] = net
    coins = _CATALOG.coins_on(net)
    rows, row = [], []
    for t in coins:
        row.append(InlineKeyboardButton(t["symbol"], callback_data=f"udc:{t['symbol']}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    await _safe_edit(q, f"Destination: *{net_name(net)}*")
    await _safe_send(context.bot, q.message.chat.id, "4️⃣ *Which coin* do you want to receive?",
                     parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))
    return US_DST_COIN


async def cb_dst_coin(update, context):
    q = update.callback_query; await _ack(q)
    sym = q.data.split(":")[1]
    r = context.user_data["route"]
    t = _CATALOG.find(sym, r["dest_net"])
    r.update({"dst_sym": sym, "dest_asset": t["assetId"]})
    await _safe_edit(q, f"Receiving: *{sym}* on {net_name(r['dest_net'])}")
    return await _ask_amount(update, context)


# ---------------- amount + blend ----------------
async def _ask_amount(update, context):
    r = context.user_data["route"]
    await _safe_send(context.bot, update.effective_chat.id,
        f"💵 How much *{r['src_sym']}* to swap? (send a number)", parse_mode=ParseMode.MARKDOWN)
    return US_AMOUNT


async def amount_text(update, context):
    raw = update.message.text.strip().replace(",", "")
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError):
        await _safe_reply(update, "⚠️ Please send a number, e.g. 25")
        return US_AMOUNT
    if amount <= 0:
        await _safe_reply(update, "⚠️ Amount must be greater than 0.")
        return US_AMOUNT
    context.user_data["amount"] = amount
    sugg = _blend(amount)
    if sugg:
        btns = [[InlineKeyboardButton(f"🫥 {a} {context.user_data['route']['src_sym']} (blend in)", callback_data=f"bl:{a}")] for a in sugg]
        btns.append([InlineKeyboardButton(f"Use my {amount}", callback_data="bl:keep")])
        await _safe_reply(update, "🫥 *Blend-In* — round amounts hide in the crowd. Round it?",
                          parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(btns))
        return US_AMOUNT
    return await _ask_recipient(update, context)


def _blend(amount):
    if amount in CROWD_AMOUNTS:
        return []
    res = []
    higher = [c for c in CROWD_AMOUNTS if c >= amount]
    if higher:
        res.append(higher[0])
    near = min(CROWD_AMOUNTS, key=lambda c: abs(c - amount))
    if near not in res:
        res.append(near)
    return res[:2]


async def cb_blend(update, context):
    q = update.callback_query; await _ack(q)
    d = q.data.split(":")[1]
    if d != "keep":
        context.user_data["amount"] = Decimal(d)
    await _safe_edit(q, f"Amount: *{context.user_data['amount']} {context.user_data['route']['src_sym']}* ✅")
    return await _ask_recipient(update, context)


# ---------------- recipient / refund via address book ----------------
async def _ask_recipient(update, context):
    r = context.user_data["route"]
    user = await _get_user(_db(context), update.effective_chat.id)
    saved = _book(user, r["dest_net"])
    chat_id = update.effective_chat.id
    if saved:
        btns = [[InlineKeyboardButton(f"📥 {_short(a)}", callback_data=f"rcp:{i}")] for i, a in enumerate(saved)]
        if len(saved) >= 2:
            btns.append([InlineKeyboardButton("🎲 Auto-rotate", callback_data="rcp:rot")])
        btns.append([InlineKeyboardButton("➕ New address", callback_data="rcp:new")])
        await _safe_send(context.bot, chat_id,
            f"📍 *Receiving {r['dst_sym']} on {net_name(r['dest_net'])}* — pick or add an address:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(btns))
        return US_RECIPIENT
    await _safe_send(context.bot, chat_id,
        f"📍 Send your *{net_name(r['dest_net'])}* address to receive the {r['dst_sym']}:",
        parse_mode=ParseMode.MARKDOWN)
    return US_RECIPIENT_TEXT


def _valid_addr(a):
    return a and " " not in a and 20 <= len(a) <= 120


async def cb_recipient(update, context):
    q = update.callback_query; await _ack(q)
    r = context.user_data["route"]
    if q.data == "rcp:new":
        await _safe_edit(q, f"Send your *{net_name(r['dest_net'])}* address:")
        return US_RECIPIENT_TEXT
    if q.data == "rcp:rot":
        context.user_data["rotate"] = True
        await _safe_edit(q, "🎲 Auto-rotate on.")
        return await _ask_refund(update, context)
    idx = int(q.data.split(":")[1])
    user = await _get_user(_db(context), q.message.chat.id)
    context.user_data["recipient"] = _book(user, r["dest_net"])[idx]
    await _safe_edit(q, f"Receiving to `{_short(context.user_data['recipient'])}` ✅")
    return await _ask_refund(update, context)


async def recipient_text(update, context):
    addr = update.message.text.strip()
    if not _valid_addr(addr):
        await _safe_reply(update, "⚠️ That doesn't look like a valid address. Try again.")
        return US_RECIPIENT_TEXT
    r = context.user_data["route"]
    context.user_data["recipient"] = addr
    await _save_addr(_db(context), update.effective_chat.id, r["dest_net"], addr)
    await _safe_reply(update, "Saved ✅")
    return await _ask_refund(update, context)


async def _ask_refund(update, context):
    r = context.user_data["route"]
    user = await _get_user(_db(context), update.effective_chat.id)
    saved = _book(user, r["origin_net"])
    chat_id = update.effective_chat.id
    if saved:
        btns = [[InlineKeyboardButton(f"↩️ {_short(a)}", callback_data=f"rfd:{i}")] for i, a in enumerate(saved)]
        btns.append([InlineKeyboardButton("➕ New address", callback_data="rfd:new")])
        await _safe_send(context.bot, chat_id,
            f"↩️ *Refund address on {net_name(r['origin_net'])}* (used if a swap fails) — pick or add:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(btns))
        return US_REFUND
    await _safe_send(context.bot, chat_id,
        f"↩️ Send your *{net_name(r['origin_net'])}* address (refunds go here if anything fails):",
        parse_mode=ParseMode.MARKDOWN)
    return US_REFUND_TEXT


async def cb_refund(update, context):
    q = update.callback_query; await _ack(q)
    r = context.user_data["route"]
    if q.data == "rfd:new":
        await _safe_edit(q, f"Send your *{net_name(r['origin_net'])}* refund address:")
        return US_REFUND_TEXT
    idx = int(q.data.split(":")[1])
    user = await _get_user(_db(context), q.message.chat.id)
    context.user_data["refund"] = _book(user, r["origin_net"])[idx]
    await _safe_edit(q, f"Refunds to `{_short(context.user_data['refund'])}` ✅")
    return await _show_privacy(update, context)


async def refund_text(update, context):
    addr = update.message.text.strip()
    if not _valid_addr(addr):
        await _safe_reply(update, "⚠️ That doesn't look like a valid address. Try again.")
        return US_REFUND_TEXT
    r = context.user_data["route"]
    context.user_data["refund"] = addr
    await _save_addr(_db(context), update.effective_chat.id, r["origin_net"], addr)
    await _safe_reply(update, "Saved ✅")
    return await _show_privacy(update, context)


# ---------------- privacy selector ----------------
def _privacy_kb(prv, origin_net):
    split = {0: "Off", 1: "2–3", 2: "3–4"}[prv["split"]]
    delay = {0: "Off", 1: "≤5 min", 2: "≤30 min"}[prv["delay"]]
    zt = "On" if prv["zt"] else "Off"
    rows = [
        [InlineKeyboardButton(f"🔀 Split: {split}", callback_data="prv:split")],
        [InlineKeyboardButton(f"⏱ Delays: {delay}", callback_data="prv:delay")],
        [InlineKeyboardButton(f"🕵️ Zero-Trace: {zt}", callback_data="prv:zt")],
    ]
    if prv["split"] > 0:
        style = prv.get("style", "multi")
        label = "Multi-address (non-custodial)" if style == "multi" else "Pay-once (custodial)"
        rows.append([InlineKeyboardButton(f"🧩 Split style: {label}", callback_data="prv:style")])
    rows.append([InlineKeyboardButton("✅ Confirm", callback_data="prv:go")])
    return InlineKeyboardMarkup(rows)


async def _show_privacy(update, context):
    prv = context.user_data.setdefault("prv", {"split": 0, "delay": 0, "zt": False, "style": "multi"})
    r = context.user_data["route"]
    await _safe_send(context.bot, update.effective_chat.id,
        f"🛡️ *Privacy options* for {context.user_data['amount']} {r['src_sym']} → {r['dst_sym']}\n"
        "Tap to toggle, then Confirm.", parse_mode=ParseMode.MARKDOWN,
        reply_markup=_privacy_kb(prv, r["origin_net"]))
    return US_PRIVACY


async def cb_privacy(update, context):
    q = update.callback_query
    prv = context.user_data.setdefault("prv", {"split": 0, "delay": 0, "zt": False, "style": "multi"})
    r = context.user_data["route"]
    action = q.data.split(":")[1]
    if action == "split":
        prv["split"] = (prv["split"] + 1) % 3
    elif action == "delay":
        prv["delay"] = (prv["delay"] + 1) % 3
    elif action == "zt":
        prv["zt"] = not prv["zt"]
    elif action == "style":
        cur = prv.get("style", "multi")
        if cur == "multi" and evm.supported(r["origin_net"]) and r.get("origin_contract"):
            prv["style"] = "custodial"
        else:
            prv["style"] = "multi"
    elif action == "go":
        await _ack(q, "Setting up…")
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return await _execute(update, context)
    await _ack(q)
    try:
        await q.edit_message_reply_markup(reply_markup=_privacy_kb(prv, r["origin_net"]))
    except Exception:
        pass
    return US_PRIVACY


# ---------------- recipients rotation + split math ----------------
async def _resolve_recipient(context, chat_id):
    ud = context.user_data
    if not ud.get("rotate"):
        return ud["recipient"]
    db = _db(context); user = await _get_user(db, chat_id)
    lst = _book(user, ud["route"]["dest_net"])
    if not lst:
        return ud.get("recipient")
    idx = int(user.get("rot_idx", 0)) % len(lst)
    await db.users.update_one({"_id": chat_id}, {"$inc": {"rot_idx": 1}}, upsert=True)
    return lst[idx]


def _split_amount(total, n):
    total = Decimal(total)
    n = max(1, min(n, int(total / SPLIT_MIN) or 1))
    if n <= 1:
        return [total.quantize(Decimal("0.01"))]
    w = [random.random() + 0.2 for _ in range(n)]
    s = sum(w)
    chunks = [(total * Decimal(str(x / s))).quantize(Decimal("0.01")) for x in w]
    chunks = [c if c >= SPLIT_MIN else SPLIT_MIN for c in chunks]
    chunks[-1] = (chunks[-1] + (total - sum(chunks))).quantize(Decimal("0.01"))
    while len(chunks) > 1 and chunks[-1] < SPLIT_MIN:
        chunks[-1] = (chunks[-1] + chunks.pop()).quantize(Decimal("0.01"))
    chunks[-1] = (chunks[-1] + (total - sum(chunks))).quantize(Decimal("0.01"))
    return chunks


# ---------------- deposit card + swap creation ----------------
async def _send_card(bot, chat_id, qr_text, caption, sid):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel transaction", callback_data=f"cxl:{sid}")]])
    try:
        await bot.send_photo(chat_id, photo=InputFile(_qr_bytes(qr_text)), caption=caption,
                             parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:
        logger.exception("card photo failed")
        try:
            await bot.send_message(chat_id, caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            logger.exception("card text failed")


async def _create_and_send(bot, db, near, chat_id, route, amount, recipient, refund,
                           gid=None, ephemeral=False, label=None):
    try:
        q = await near.quote(route["origin_asset"], route["dest_asset"], amount,
                             route["origin_decimals"], recipient, refund)
    except Exception as e:
        logger.exception("quote failed")
        await _safe_send(bot, chat_id, f"❌ Couldn't create a deposit for {amount} {route['src_sym']}: {_friendly_err(e)}")
        return
    deposit = q["deposit_address"]
    link = _payment_link(route.get("origin_contract"), route["origin_net"], deposit, amount, route["origin_decimals"])
    sid = secrets.token_hex(4)
    await db.swaps.insert_one({
        "sid": sid, "gid": gid, "chat_id": chat_id,
        "deposit_address": deposit, "deposit_memo": q.get("deposit_memo"),
        "recipient": recipient, "refund": refund,
        "amount_in": str(amount), "amount_out": q.get("amount_out_formatted"),
        "src_sym": route["src_sym"], "src_net": route["origin_net"],
        "dst_sym": route["dst_sym"], "dst_net": route["dest_net"],
        "status": "PENDING_DEPOSIT", "ephemeral": ephemeral,
        "correlation_id": q.get("correlation_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    memo = f"\n*Memo:* `{q['deposit_memo']}`" if q.get("deposit_memo") else ""
    lbl = f" — chunk {label[0]}/{label[1]}" if label else ""
    zt = "\n🕵️ Zero-Trace: self-destructs after completion." if ephemeral else ""
    cap = (f"✅ *Deposit ready*{lbl}\n\n"
           f"Send *{amount} {route['src_sym']}* on *{net_name(route['origin_net'])}* to:\n`{deposit}`{memo}\n\n"
           f"You'll receive *~{q.get('amount_out_formatted','?')} {route['dst_sym']}* "
           f"(~${q.get('amount_out_usd','?')}) on {net_name(route['dest_net'])}\n→ `{_short(recipient)}`\n"
           f"⏱ ETA ~{q.get('time_estimate','?')}s after deposit confirms\n🔒 Single-use address.{zt}")
    await _send_card(bot, chat_id, link, cap, sid)
    await _safe_send(bot, chat_id, f"👇 Tap to copy the address:\n`{deposit}`", parse_mode=ParseMode.MARKDOWN)


async def _split_job(context):
    j = context.job; d = j.data; app = context.application
    if d["gid"] in app.bot_data.setdefault("cancelled_gids", set()):
        return
    await _safe_send(context.bot, j.chat_id, f"⏱ Time for chunk {d['idx']}/{d['total']} — {d['amount']} {d['route']['src_sym']}:")
    await _create_and_send(context.bot, app.bot_data["db"], app.bot_data["near"], j.chat_id,
                           d["route"], Decimal(d["amount"]), d["recipient"], d["refund"],
                           gid=d["gid"], ephemeral=d["ephemeral"], label=(d["idx"], d["total"]))


async def _execute(update, context):
    chat_id = update.effective_chat.id
    ud = context.user_data
    route = ud["route"]; amount = ud["amount"]; refund = ud["refund"]
    prv = ud.get("prv", {"split": 0, "delay": 0, "zt": False, "style": "multi"})
    ephemeral = bool(prv.get("zt"))
    db = _db(context); near = _near(context)
    n = {0: 1, 1: random.randint(2, 3), 2: random.randint(3, 4)}[prv.get("split", 0)]

    # custodial pay-once split
    if n > 1 and prv.get("style") == "custodial" and evm.supported(route["origin_net"]) and route.get("origin_contract"):
        await _start_custodial(context, chat_id, route, amount, n, ephemeral)
        ud.clear()
        return ConversationHandler.END

    if n == 1:
        rec = await _resolve_recipient(context, chat_id)
        await _create_and_send(context.bot, db, near, chat_id, route, amount, rec, refund, ephemeral=ephemeral)
        ud.clear()
        return ConversationHandler.END

    # multi-address non-custodial split
    chunks = _split_amount(amount, n)
    recipients = [await _resolve_recipient(context, chat_id) for _ in chunks]
    gid = secrets.token_hex(4)
    delay_max = {0: 0, 1: 300, 2: 1800}[prv.get("delay", 0)]
    summary = (f"🔀 *Split plan* — {len(chunks)} chunks of {route['src_sym']}:\n"
               + "\n".join(f"  • {c}" for c in chunks)
               + ("\n\nI'll ping you for each chunk." if delay_max else "\n\nFresh address each — send in any order."))
    await _safe_send(context.bot, chat_id, summary, parse_mode=ParseMode.MARKDOWN,
                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel remaining", callback_data=f"cxlp:{gid}")]]))
    bot = context.bot
    immediate = [(chunks[0], recipients[0], 1)]
    cum = 0
    for i, (c, rec) in enumerate(zip(chunks[1:], recipients[1:]), start=2):
        if delay_max:
            cum += random.randint(30, delay_max)
            context.job_queue.run_once(_split_job, when=cum, chat_id=chat_id, name=gid,
                data={"amount": str(c), "recipient": rec, "refund": refund, "route": route,
                      "ephemeral": ephemeral, "gid": gid, "idx": i, "total": len(chunks)})
        else:
            immediate.append((c, rec, i))

    async def _bg():
        for c, rec, idx in immediate:
            await _create_and_send(bot, db, near, chat_id, route, c, rec, refund,
                                   gid=gid, ephemeral=ephemeral, label=(idx, len(chunks)))
    asyncio.create_task(_bg())
    ud.clear()
    return ConversationHandler.END


# ---------------- custodial pay-once ----------------
async def _start_custodial(context, chat_id, route, amount, n, ephemeral):
    db = _db(context)
    recipient = await _resolve_recipient(context, chat_id)
    refund = context.user_data["refund"]
    address, pk = evm.new_wallet()
    chunks = [str(c) for c in _split_amount(amount, n)]
    await db.custodial.insert_one({
        "chat_id": chat_id, "address": address, "pk": pk, "network": route["origin_net"],
        "token_addr": route["origin_contract"], "decimals": route["origin_decimals"],
        "total": str(amount), "chunks": chunks, "recipient": recipient, "refund": refund,
        "route": route, "ephemeral": ephemeral, "dispatched": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    link = _payment_link(route["origin_contract"], route["origin_net"], address, amount, route["origin_decimals"])
    cap = (f"🧩 *Pay-once split (custodial)* — {n} chunks\n\n"
           f"Send *{amount} {route['src_sym']}* on *{net_name(route['origin_net'])}* to your one-time wallet:\n`{address}`\n\n"
           f"➕ Also send a little *native gas* (e.g. ETH) to it so I can forward the chunks.\n\n"
           f"When funded, I auto-split into {n} random chunks (each a fresh route) → {route['dst_sym']} on {net_name(route['dest_net'])}.")
    await _send_card(context.bot, chat_id, link, cap, "custodial")
    await _safe_send(context.bot, chat_id,
        "🔑 *RECOVERY KEY* — save this now. If anything gets stuck, import it into any wallet to recover funds:\n"
        f"`{pk}`\n\n⚠️ Anyone with this key controls that one-time wallet. It only holds funds mid-swap.",
        parse_mode=ParseMode.MARKDOWN)


async def _custodial_loop(app):
    db = app.bot_data["db"]; near = app.bot_data["near"]
    while True:
        try:
            jobs = await db.custodial.find({"dispatched": False}).to_list(50)
            for j in jobs:
                try:
                    net = j["network"]
                    bal = await asyncio.to_thread(evm.erc20_balance, net, j["token_addr"], j["decimals"], j["address"])
                    gas = await asyncio.to_thread(evm.native_balance, net, j["address"])
                    if bal < Decimal(j["total"]) or gas <= 0:
                        continue
                    await db.custodial.update_one({"_id": j["_id"]}, {"$set": {"dispatched": True}})
                    await _safe_send(app.bot, j["chat_id"],
                        f"💰 Received {j['total']} {j['route']['src_sym']} + gas. Splitting into {len(j['chunks'])} chunks now…")
                    for idx, amt in enumerate(j["chunks"], start=1):
                        try:
                            q = await near.quote(j["route"]["origin_asset"], j["route"]["dest_asset"],
                                                 amt, j["decimals"], j["recipient"], j["address"])
                            txh = await asyncio.to_thread(evm.send_erc20, net, j["pk"], j["token_addr"],
                                                         j["decimals"], q["deposit_address"], amt)
                            await db.swaps.insert_one({
                                "sid": secrets.token_hex(4), "gid": str(j["_id"]), "chat_id": j["chat_id"],
                                "deposit_address": q["deposit_address"], "recipient": j["recipient"],
                                "refund": j["address"], "amount_in": amt, "amount_out": q.get("amount_out_formatted"),
                                "src_sym": j["route"]["src_sym"], "src_net": net,
                                "dst_sym": j["route"]["dst_sym"], "dst_net": j["route"]["dest_net"],
                                "status": "PROCESSING", "ephemeral": j.get("ephemeral", False),
                                "created_at": datetime.now(timezone.utc).isoformat(),
                            })
                            await _safe_send(app.bot, j["chat_id"], f"➡️ Chunk {idx}/{len(j['chunks'])} sent ({amt} {j['route']['src_sym']}). tx `{txh}`", parse_mode=ParseMode.MARKDOWN)
                        except Exception as e:
                            logger.exception("custodial chunk dispatch failed")
                            await _safe_send(app.bot, j["chat_id"],
                                f"⚠️ Chunk {idx} failed: {_friendly_err(e)}. Remaining funds are safe in your one-time wallet — use your recovery key if needed.")
                except Exception:
                    logger.exception("custodial job error")
        except Exception:
            logger.exception("custodial loop error")
        await asyncio.sleep(20)


# ---------------- cancel ----------------
async def cb_cancel(update, context):
    q = update.callback_query
    sid = q.data.split(":", 1)[1]
    db = _db(context)
    swap = await db.swaps.find_one({"sid": sid, "chat_id": q.message.chat.id})
    if not swap:
        await _ack(q, "Not found.", show_alert=True)
        return
    if swap.get("status") == "PENDING_DEPOSIT":
        await db.swaps.update_one({"_id": swap["_id"]}, {"$set": {"status": "CANCELLED"}})
        await _ack(q, "Cancelled.")
        try:
            await q.edit_message_caption(caption="✖️ *Cancelled.* Don't send funds to that address.", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
    else:
        await _ack(q, "Too late — a deposit was already detected.", show_alert=True)


async def cb_cancel_plan(update, context):
    q = update.callback_query
    gid = q.data.split(":", 1)[1]
    db = _db(context)
    context.application.bot_data.setdefault("cancelled_gids", set()).add(gid)
    await db.swaps.update_many({"gid": gid, "status": "PENDING_DEPOSIT"}, {"$set": {"status": "CANCELLED"}})
    try:
        for jb in context.job_queue.get_jobs_by_name(gid):
            jb.schedule_removal()
    except Exception:
        pass
    await _ack(q, "Cancelled.")
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _safe_send(context.bot, q.message.chat.id, "✖️ Remaining chunks cancelled.")


async def cancel(update, context):
    context.user_data.clear()
    await _safe_reply(update, "Cancelled. /start to begin again.")
    return ConversationHandler.END


# ---------------- natural language entry ----------------
async def nl_text(update, context):
    text = update.message.text or ""
    res = nlp.parse(text, _CATALOG)
    if not res.get("ok"):
        if res.get("error"):
            await _safe_reply(update, f"🤔 {res['error']}")
        return  # not a command
    context.user_data.clear()
    r = {
        "origin_net": res["src_net"], "src_sym": res["src_sym"],
        "origin_asset": res["src_tok"]["assetId"], "origin_decimals": res["src_tok"]["decimals"],
        "origin_contract": res["src_tok"].get("contractAddress"),
        "dest_net": res["dst_net"], "dst_sym": res["dst_sym"], "dest_asset": res["dst_tok"]["assetId"],
    }
    context.user_data["route"] = r
    await _safe_reply(update,
        f"🌀 *{res['src_sym']}* ({net_name(res['src_net'])}) → *{res['dst_sym']}* ({net_name(res['dst_net'])})",
        parse_mode=ParseMode.MARKDOWN)
    if res.get("amount"):
        context.user_data["amount"] = res["amount"]
        return await _ask_recipient(update, context)
    return await _ask_amount(update, context)


# ---------------- status poller ----------------
async def _poller(app):
    db = app.bot_data["db"]; near = app.bot_data["near"]
    labels = {
        "KNOWN_DEPOSIT_TX": "📥 Deposit detected — swapping now…",
        "PROCESSING": "⏳ Swapping your funds…",
        "SUCCESS": "✅ Done! Funds delivered on the destination chain.",
        "REFUNDED": "↩️ Swap refunded to your refund address.",
        "FAILED": "❌ Swap failed — funds refunded to your refund address.",
        "INCOMPLETE_DEPOSIT": "⚠️ Partial deposit — send the remaining amount.",
    }
    while True:
        try:
            active = await db.swaps.find({"status": {"$nin": list(TERMINAL_STATUSES) + ["CANCELLED"]}}).to_list(200)
            for s in active:
                try:
                    res = await near.get_status(s["deposit_address"], s.get("deposit_memo"))
                except Exception:
                    continue
                ns = res["status"]
                if ns != s.get("status"):
                    await db.swaps.update_one({"_id": s["_id"]}, {"$set": {"status": ns}})
                    msg = labels.get(ns)
                    if msg:
                        await _safe_send(app.bot, s["chat_id"], msg)
                    if s.get("ephemeral") and ns in TERMINAL_STATUSES:
                        await db.swaps.delete_one({"_id": s["_id"]})
        except Exception:
            logger.exception("poller error")
        await asyncio.sleep(15)


def start_pollers(app):
    global _poll_task, _custodial_task
    _poll_task = asyncio.create_task(_poller(app))
    _custodial_task = asyncio.create_task(_custodial_loop(app))


async def stop_pollers():
    for t in (_poll_task, _custodial_task):
        if t:
            t.cancel()


async def _on_error(update, context):
    logger.error("handler error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(update.effective_chat.id,
                "⚠️ Something went wrong. Please try again or /start to restart.")
    except Exception:
        pass


async def conv_timeout(update, context):
    context.user_data.clear()
    if update and update.effective_chat:
        await _safe_send(context.bot, update.effective_chat.id, "⌛ Session timed out. /start to begin again.")
    return ConversationHandler.END


# ---------------- app factory ----------------
def create_application(token, db, near: NearBridgeClient) -> Application:
    global _CATALOG
    _CATALOG = near.catalog
    app = Application.builder().token(token).updater(None).build()
    app.bot_data["db"] = db
    app.bot_data["near"] = near
    app.add_error_handler(_on_error)

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_mode, pattern="^mode:"),
            CommandHandler("swap", cmd_swap),
            MessageHandler(filters.TEXT & ~filters.COMMAND, nl_text),
        ],
        states={
            US_SRC_NET: [CallbackQueryHandler(cb_src_net, pattern="^usn:")],
            US_SRC_COIN: [CallbackQueryHandler(cb_src_coin, pattern="^usc:")],
            US_DST_NET: [CallbackQueryHandler(cb_dst_net, pattern="^udn:")],
            US_DST_COIN: [CallbackQueryHandler(cb_dst_coin, pattern="^udc:")],
            US_AMOUNT: [CallbackQueryHandler(cb_blend, pattern="^bl:"),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, amount_text)],
            US_RECIPIENT: [CallbackQueryHandler(cb_recipient, pattern="^rcp:")],
            US_RECIPIENT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, recipient_text)],
            US_REFUND: [CallbackQueryHandler(cb_refund, pattern="^rfd:")],
            US_REFUND_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, refund_text)],
            US_PRIVACY: [CallbackQueryHandler(cb_privacy, pattern="^prv:")],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, conv_timeout), CallbackQueryHandler(conv_timeout)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", cmd_start)],
        per_message=False,
        conversation_timeout=600,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("addresses", cmd_addresses))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CallbackQueryHandler(cb_clear, pattern="^clr:"))
    app.add_handler(CallbackQueryHandler(cb_show_book, pattern="^show:book$"))
    app.add_handler(CallbackQueryHandler(cb_cancel, pattern="^cxl:"))
    app.add_handler(CallbackQueryHandler(cb_cancel_plan, pattern="^cxlp:"))
    app.add_handler(CallbackQueryHandler(cb_delete, pattern="^del:"))
    return app
