# RelayBridge — Base → Starknet Auto-Bridge Telegram Bot

## Problem Statement
User wants a "crypto wallet" that: when USDC is sent on Base, auto-converts it to funds on Starknet and delivers to the user's Starknet address. Pivoted to a **Telegram bot** (free) instead of a website. Real mainnet.

## Key Decisions
- **Bridge:** NEAR Intents (1-Click) now; Layerswap to be wired in later (needs API key).
- **Route reality:** NEAR Intents does NOT currently support USDC *on Starknet* as a destination (only STRK/ZEC/XRP). So bot bridges **Base USDC → Starknet STRK** for now. USDC→USDC on Starknet is only available via Layerswap (deferred until user provides key).
- **Architecture:** No private-key custody. Each bridge creates a fresh single-use NEAR Intents deposit address bound to the user's Starknet recipient. User sends USDC on Base to it; bridge auto-delivers to Starknet. Receiving is gasless.
- **Privacy:** fresh deposit address per swap, minimal data retention, `/forget` wipes all user data, httpx token logging disabled.

## Tech Stack
- Backend: FastAPI (supervisor, port 8001), python-telegram-bot 21.9 (webhook mode), motor/MongoDB, httpx.
- Bot webhook: `{PUBLIC_BASE_URL}/api/telegram/webhook/{secret}`.
- Frontend: React landing page (dark "tactical DeFi" theme) promoting the bot with live QR + stats.
- Bridge API: NEAR Intents 1-Click `https://1click.chaindefuser.com` (`/v0/tokens`, `/v0/quote` dry=false, `/v0/status`).

## Files
- `backend/near_client.py` — NEAR Intents client (origin=Base USDC, dest=Starknet STRK, configurable).
- `backend/bot.py` — Telegram handlers: /start /bridge /addresses /history /privacy /forget, ConversationHandler, background status poller.
- `backend/server.py` — FastAPI app, bot lifecycle (initialize/start/set_webhook/set_commands), webhook endpoint, /api/bot-info, /api/stats.
- `frontend/src/App.js` + `App.css` — landing page.

## Implemented (2026-09-17)
- ✅ Telegram bot @swaswabotbot live; webhook set; command menu.
- ✅ /bridge flow: pick/enter Starknet recipient → pick/enter Base refund → amount → live NEAR quote → deposit address + QR + shows USDC in and ~STRK out (+USD value) + ETA.
- ✅ /invoice <amount>: merchant flow — creates a shareable payment card with EIP-681 QR (pre-fills token, Base network, amount) bound to a fresh deposit address; tap-to-copy address + payment link; client pays USDC on Base, funds auto-arrive on Starknet; invoice-specific alerts ("Client paid X USDC...").
- ✅ Address saver (per user, Starknet + Base) with /addresses management (delete).
- ✅ /history recent bridges; background poller (15s) notifies on deposit detected / processing / success / refunded / failed.
- ✅ /privacy and /forget (data wipe).
- ✅ Landing page with live bot QR + stats. Verified end-to-end by real user (quote 201, QR delivered, swap persisted 1 USDC → ~33.78 STRK).
- ✅ /invoice verified via webhook sim with seeded user (5 USDC → ~173.3 STRK, is_invoice row persisted, poller tracking).

## Backlog / Next
- P0: Wire Layerswap for true USDC→USDC on Starknet (needs Mainnet API key from user).

## Fixes / Hardening (2026-09-17)
- 🐞 FIXED `/invoice`: inline-button callback_data held full address/link (>64B) → Telegram `Button_data_invalid` → card never sent. Now uses short `cxl:<sid>` (12B). Verified by testing_agent (no Button_data_invalid; invoice row with sid/is_invoice persists).
- ✅ Added **Cancel transaction** button on every /bridge and /invoice card (`cxl:<sid>` → sets swap status CANCELLED; poller stops tracking; blocks if deposit already detected). Verified.
- ✅ Robustness (no-hang): global error handler messages the user; `_send_deposit_card` falls back to text if photo send fails; ConversationHandler `conversation_timeout=300s` with TIMEOUT handler; friendly timeout/min-amount error messages; bridge_start send wrapped so state always advances; httpx token logging disabled.
- ✅ Bridge/invoice QR now encodes EIP-681 payment link so wallets auto-fill token+network+amount on scan; tap-to-copy address/link via monospace.
- ✅ Deployment health check: PASS (no blockers).
- P1: Show USDC-equivalent value more prominently; provider toggle (NEAR vs Layerswap).
- P1: Explorer links (basescan/starkscan) + swap detail in /history.
- P2: Minimum-received / slippage display; refund status detail; per-user default address quick-pick.

## Notes
- MOCKED: nothing is mocked. Real mainnet NEAR Intents route. Users must send real USDC on Base; funds are real.
