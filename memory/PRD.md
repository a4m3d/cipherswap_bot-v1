# CipherSwap (formerly RelayBridge) — Private cross-chain swap Telegram bot

> 2026-09-17 rebuild: /start now has 2 modes — Classic (Base USDC→Starknet STRK) and Universal (any coin, 35 networks, 196 assets via NEAR Intents). Natural-language commands, address book, both split styles (multi-address + custodial pay-once), zero-trace, /clear. Verified 13/13 by testing agent.

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

## Privacy Suite + Invoice Removal (2026-09-17)
- ❌ Removed the /invoice feature entirely (command, handlers, help, poller branch).
- ✅ Added in-flow **Privacy options** selector inside /bridge (tap-to-toggle, then Confirm):
  - 🫥 **Blend-In Amounts** — suggests rounding to crowd amounts (5/10/25/50/100/250/500/1000).
  - 🎲 **Rotate receiving wallet** — auto-rotates across the user's saved Starknet addresses (per bridge and per split-chunk); tracked via users.rot_idx.
  - 🔀 **Split** — off / 2–3 / 3–4 random chunks (each a fresh deposit address); non-custodial.
  - ⏱ **Delays** — off / ≤5m / ≤30m; when on, chunks are scheduled via JobQueue and the bot pings when to send the next.
  - 🕵️ **Zero-Trace** — swap stored with ephemeral=true; record auto-deletes on terminal status in the poller.
- ✅ **Cancel remaining plan** button (cxlp:<gid>) cancels all pending chunks in a split group and removes scheduled jobs; per-tx Cancel (cxl:<sid>) retained.
- ✅ Robustness/no-hang: ALL Telegram sends/acks/edits wrapped (_ack, _safe_edit, _safe_send, _safe_reply, _send_deposit_card fallback) so a failed/stale Telegram call can never abort a conversation; conversation_timeout=300s.
- Verified E2E via webhook + Mongo: split (2 chunks sum=total), rotation (distinct recipients), blend applied, zero-trace ephemeral, cancel-plan → CANCELLED, single bridge.
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

## Update — 2026-06 (Pro UX rebuild: single-wizard nav, notifications, auto-recovery)
User asks addressed: tx hashes after transfer, timely notifications, never-get-stuck auto-recovery, post-success "what next", clearer/less-scattered menu, Back/Forward navigation at every step. Chosen extras: rate-lock warning, favorites, custodial recovery reminder.
- ✅ **Single edit-in-place wizard** — the whole guided flow (Step 1/5 source net → send coin → dest net → receive coin → amount → recipient → refund → Step 5/5 privacy) lives in ONE message that edits itself; breadcrumb shows selections so far. No more scattered messages.
- ✅ **Back at every step** via `nav:<step>` (srcnet/srccoin/dstnet/dstcoin/amount/recipient/refund) — wrong chain/coin no longer means restart.
- ✅ **Clean main menu** — 🚀 Start a swap · ⚡ Quick Base USDC→Starknet · ⭐ favorites · Addresses/History · Privacy/Clear. Networks/coins ordered popular-first.
- ✅ **Tx-hash notifications** — near_client.get_status now parses swapDetails.originChainTxHashes/destinationChainTxHashes (hash + explorerUrl). Poller posts clickable explorer links on deposit-detected and delivered; custodial chunk sends link via evm.explorer_tx().
- ✅ **Timely lifecycle pings** — deposit detected → swapping → delivered (exact amount + USD + tx link) → refunded/failed (with reason); 15-min "still waiting" nudge; slow-swap heads-up; rate-expired warning with 🔄 Refresh-rate (`rq:<sid>` re-quotes fresh address).
- ✅ **What-next after success** — 🔁 Same again · 🔀 Reverse · 🌀 New swap · ⭐ Save route (`rpt:`/`rev:`/`start:uni`/`savefav:`).
- ✅ **Favorites** — savefav pushes {label, route} (capped 8) to users.favorites; menu shows fav buttons; `fav:i` one-taps into amount step.
- ✅ **Auto-recovery** — global error handler & conversation timeout show a ▶️ Continue button (`menu:open`) that resets state and reopens the menu; ConversationHandler `allow_reentry=True`; NL entry-point gated by verb regex so it no longer cannibalizes in-state text handlers (root-cause bug fixed).
- ✅ **Custodial recovery reminder** — pay-once wallet unfunded after 30 min → one reminder listing what's still needed + recovery-key hint.
- Verified: pytest suite **23/23 PASSED** (iteration_4.json) after fixing the entry_point/allow_reentry cannibalization bug. Bot @swaswabotbot live, catalog 196 assets.
- Backlog/next: X-Telegram-Bot-Api-Secret-Token header check; larger split totals vs NEAR $1000/route min; consider splitting bot.py into modules; Layerswap for true USDC→USDC Starknet (needs user API key).
