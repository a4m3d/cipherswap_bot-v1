"""Telegram bot: auto-bridge USDC (Base) -> STRK (Starknet) via NEAR Intents.

Privacy-first: a fresh deposit address is generated for every swap, we store
the minimum needed, and users can wipe all their data with /forget.
"""
import asyncio
import io
import logging
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
BR_RECIPIENT, BR_RECIPIENT_TEXT, BR_REFUND, BR_REFUND_TEXT, BR_AMOUNT = range(5)

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


async def cmd_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a shareable client invoice: /invoice 50"""
    db = _db(context)
    near = _near(context)
    chat_id = update.effective_chat.id
    user = await _get_user(db, chat_id)
    sn = user.get("starknet", [])

    if not context.args:
        await update.message.reply_text(
            "Usage: `/invoice <amount_usdc>`\nExample: `/invoice 50`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        amount = Decimal(context.args[0].replace(",", ""))
    except (InvalidOperation, ValueError):
        await update.message.reply_text("⚠️ Invalid amount. Example: /invoice 50")
        return
    if amount < MIN_USDC:
        await update.message.reply_text(f"⚠️ Minimum is {MIN_USDC} USDC.")
        return
    if not sn:
        await update.message.reply_text(
            "You need a Starknet receiving address first. Run /bridge once to save it."
        )
        return

    recipient = sn[0]
    refund = (user.get("base") or [recipient])[0]
    try:
        quote = await near.create_swap(amount, recipient, refund)
    except Exception as e:
        logger.exception("invoice quote failed")
        await update.message.reply_text(f"❌ Couldn't create the invoice: {_friendly_err(e)}")
        return

    deposit = quote["deposit_address"]
    link = _payment_link(deposit, amount)
    sid = secrets.token_hex(4)
    await db.swaps.insert_one({
        "sid": sid,
        "chat_id": chat_id,
        "deposit_address": deposit,
        "deposit_memo": quote.get("deposit_memo"),
        "recipient": recipient,
        "refund": refund,
        "amount_in": str(amount),
        "amount_out": quote.get("amount_out_formatted"),
        "status": "PENDING_DEPOSIT",
        "is_invoice": True,
        "correlation_id": quote.get("correlation_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    memo_line = f"\nMemo: `{quote['deposit_memo']}`" if quote.get("deposit_memo") else ""
    caption = (
        f"🧾 *Invoice — {amount} USDC on Base*\n\n"
        f"Deposit address:\n`{deposit}`{memo_line}\n\n"
        "Scan the QR — it pre-fills token, network & amount in the client's wallet.\n"
        "_Forward this to your client. They pay USDC on Base; it auto-arrives on your Starknet._"
    )
    await _send_deposit_card(context.bot, chat_id, link, caption, sid)
    await context.bot.send_message(
        chat_id,
        f"👇 Tap to copy the address:\n`{deposit}`\n\n👇 Tap to copy the payment link:\n`{link}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cb_copy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Copied below — tap to copy again anytime.", show_alert=False)


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
        await bot.send_message(chat_id, caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def cb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    sid = q.data.split(":", 1)[1]
    db = _db(context)
    swap = await db.swaps.find_one({"sid": sid, "chat_id": q.message.chat.id})
    if not swap:
        await q.answer("This transaction was not found.", show_alert=True)
        return
    if swap.get("status") == "PENDING_DEPOSIT":
        await db.swaps.update_one({"_id": swap["_id"]}, {"$set": {"status": "CANCELLED"}})
        await q.answer("Transaction cancelled.")
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
        await q.answer("Too late to cancel — a deposit was already detected on-chain.", show_alert=True)


# ---------- basic commands ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    near = _near(context)
    text = (
        "🛰️ *Base → Starknet Auto-Bridge*\n\n"
        f"Send *{near.origin_symbol}* on *Base*, receive *{near.dest_symbol}* on *Starknet* "
        "at your address — automatically.\n\n"
        "Tap /bridge to start. Each bridge uses a *brand-new deposit address* for privacy.\n\n"
        "Commands:\n"
        "• /bridge — start a new bridge\n"
        "• /invoice <amount> — create a client payment invoice\n"
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
        "🔒 *Your privacy*\n\n"
        "• A *fresh deposit address* is created for every single bridge — your wallets are never reused or linked on-chain.\n"
        "• Funds route through the bridge's liquidity network, breaking the direct A→B trail between your Base and Starknet wallets.\n"
        "• We store only what's needed to track your active swaps. Use /forget any time to erase everything.\n\n"
        "⚠️ Honest note: no bridge can make transactions *100%* untraceable. This setup gives strong hygiene, "
        "but sophisticated chain analysis can never be fully defeated. For maximum privacy, use a fresh Starknet address per bridge."
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
    await q.answer()
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


async def cb_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "sn:new":
        await q.edit_message_text("Send me your *Starknet* address (0x...).", parse_mode=ParseMode.MARKDOWN)
        return BR_RECIPIENT_TEXT
    idx = int(q.data.split(":")[1])
    user = await _get_user(_db(context), q.message.chat.id)
    context.user_data["recipient"] = user["starknet"][idx]
    await q.edit_message_text(f"Receiving to `{_short(context.user_data['recipient'])}` ✅", parse_mode=ParseMode.MARKDOWN)
    return await _ask_refund(update, context)


async def recipient_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = update.message.text.strip()
    if not STARKNET_RE.match(addr):
        await update.message.reply_text("⚠️ That doesn't look like a Starknet address (0x + hex). Try again.")
        return BR_RECIPIENT_TEXT
    context.user_data["recipient"] = addr
    await _save_address(_db(context), update.effective_chat.id, "starknet", addr)
    await update.message.reply_text("Saved Starknet address ✅")
    return await _ask_refund(update, context)


async def _ask_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = _db(context)
    user = await _get_user(db, update.effective_chat.id)
    base = user.get("base", [])
    chat = update.effective_chat
    if base:
        buttons = [[InlineKeyboardButton(f"↩️ {_short(a)}", callback_data=f"bs:{i}")] for i, a in enumerate(base)]
        buttons.append([InlineKeyboardButton("➕ Enter a new address", callback_data="bs:new")])
        await context.bot.send_message(
            chat.id,
            "Which *Base* address are you sending *from*? (used for refunds if anything fails)",
            parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons),
        )
        return BR_REFUND
    await context.bot.send_message(
        chat.id,
        "Now send your *Base* address (the wallet you'll send USDC *from* — used for refunds). 0x + 40 hex.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return BR_REFUND_TEXT


async def cb_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "bs:new":
        await q.edit_message_text("Send me your *Base* refund address (0x + 40 hex).", parse_mode=ParseMode.MARKDOWN)
        return BR_REFUND_TEXT
    idx = int(q.data.split(":")[1])
    user = await _get_user(_db(context), q.message.chat.id)
    context.user_data["refund"] = user["base"][idx]
    await q.edit_message_text(f"Refunds to `{_short(context.user_data['refund'])}` ✅", parse_mode=ParseMode.MARKDOWN)
    return await _ask_amount(update, context)


async def refund_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = update.message.text.strip()
    if not EVM_RE.match(addr):
        await update.message.reply_text("⚠️ That's not a valid Base (EVM) address. Send 0x + 40 hex characters.")
        return BR_REFUND_TEXT
    context.user_data["refund"] = addr
    await _save_address(_db(context), update.effective_chat.id, "base", addr)
    await update.message.reply_text("Saved Base address ✅")
    return await _ask_amount(update, context)


async def _ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        update.effective_chat.id,
        f"💵 How much *USDC* do you want to bridge? (minimum {MIN_USDC})",
        parse_mode=ParseMode.MARKDOWN,
    )
    return BR_AMOUNT


async def amount_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().replace(",", "")
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError):
        await update.message.reply_text("⚠️ Please send a number, e.g. 25")
        return BR_AMOUNT
    if amount < MIN_USDC:
        await update.message.reply_text(f"⚠️ Minimum is {MIN_USDC} USDC.")
        return BR_AMOUNT

    near = _near(context)
    recipient = context.user_data["recipient"]
    refund = context.user_data["refund"]
    wait = await update.message.reply_text("🔎 Getting you a fresh deposit address...")
    try:
        quote = await near.create_swap(amount, recipient, refund)
    except Exception as e:
        logger.exception("quote failed")
        await wait.edit_text(f"❌ Couldn't create the bridge: {_friendly_err(e)}")
        context.user_data.clear()
        return ConversationHandler.END

    db = _db(context)
    chat_id = update.effective_chat.id
    deposit = quote["deposit_address"]
    link = _payment_link(deposit, amount)
    sid = secrets.token_hex(4)
    await db.swaps.insert_one({
        "sid": sid,
        "chat_id": chat_id,
        "deposit_address": deposit,
        "deposit_memo": quote.get("deposit_memo"),
        "recipient": recipient,
        "refund": refund,
        "amount_in": str(amount),
        "amount_out": quote.get("amount_out_formatted"),
        "status": "PENDING_DEPOSIT",
        "correlation_id": quote.get("correlation_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    memo_line = f"\n*Memo:* `{quote['deposit_memo']}`" if quote.get("deposit_memo") else ""
    eta = quote.get("time_estimate")
    caption = (
        "✅ *Deposit address ready*\n\n"
        f"Send exactly *{amount} USDC* on *Base* to:\n`{deposit}`{memo_line}\n\n"
        f"You'll receive *~{quote.get('amount_out_formatted','?')} STRK* "
        f"(~${quote.get('amount_out_usd','?')}) on Starknet\n"
        f"→ `{_short(recipient)}`\n\n"
        f"⏱ Est. arrival: ~{eta}s after your deposit confirms\n"
        "🔒 Single-use address. Scan to auto-fill the amount in your wallet."
    )
    try:
        await wait.delete()
    except Exception:
        pass
    await _send_deposit_card(context.bot, chat_id, link, caption, sid)
    await context.bot.send_message(
        chat_id,
        f"👇 Tap to copy the address:\n`{deposit}`",
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text("Cancelled. Tap /bridge whenever you're ready.")
    return ConversationHandler.END


async def cb_go_bridge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry from the inline 'Start a bridge' button."""
    await update.callback_query.answer()
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
                    is_inv = s.get("is_invoice")
                    amt = s.get("amount_in")
                    if is_inv:
                        msg_map = {
                            "KNOWN_DEPOSIT_TX": f"💰 Client paid {amt} USDC — bridging to your Starknet now...",
                            "PROCESSING": f"⏳ Bridging client payment of {amt} USDC...",
                            "SUCCESS": f"✅ Client payment of {amt} USDC arrived on your Starknet.",
                            "REFUNDED": f"↩️ Client payment of {amt} USDC was refunded.",
                            "FAILED": f"❌ Client payment of {amt} USDC failed/refunded.",
                            "INCOMPLETE_DEPOSIT": f"⚠️ Client partially paid {amt} USDC.",
                        }
                        msg = msg_map.get(new_status)
                    else:
                        msg = labels.get(new_status)
                    if msg:
                        try:
                            await app.bot.send_message(s["chat_id"], msg)
                        except Exception:
                            logger.warning("could not notify chat %s", s["chat_id"])
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
    app.add_handler(CommandHandler("invoice", cmd_invoice))
    app.add_handler(CallbackQueryHandler(cb_cancel, pattern="^cxl:"))
    app.add_handler(CallbackQueryHandler(cb_delete, pattern="^del:"))
    return app
