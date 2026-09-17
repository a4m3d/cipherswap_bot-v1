"""NEAR Intents (1-Click) bridge client.

Bridges an EXACT_INPUT amount of USDC on Base to a destination token on
Starknet. Destination is currently STRK because 1-Click does not yet expose
USDC on Starknet. Kept configurable so we can swap to a USDC route (or
Layerswap) later without touching the bot code.
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

BASE_URL = os.environ.get("NEAR_INTENTS_BASE", "https://1click.chaindefuser.com")
JWT = os.environ.get("NEAR_INTENTS_JWT", "").strip()

# Origin: native USDC on Base (6 decimals)
ORIGIN_ASSET = "nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near"
ORIGIN_SYMBOL = "USDC"
ORIGIN_DECIMALS = 6

# Destination: STRK on Starknet (18 decimals)
DEST_ASSET = "nep141:starknet.omft.near"
DEST_SYMBOL = "STRK"
DEST_DECIMALS = 18

TERMINAL_STATUSES = {"SUCCESS", "REFUNDED", "FAILED"}


class BridgeError(Exception):
    pass


def _headers():
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if JWT:
        h["Authorization"] = f"Bearer {JWT}"
    return h


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class NearBridgeClient:
    def __init__(self):
        self.origin_symbol = ORIGIN_SYMBOL
        self.dest_symbol = DEST_SYMBOL

    async def _request(self, method: str, path: str, **kwargs):
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=25) as c:
            r = await c.request(method, path, headers=_headers(), **kwargs)
        if r.status_code >= 400:
            raise BridgeError(f"Bridge API {r.status_code}: {r.text[:400]}")
        return r.json()

    async def create_swap(self, amount_usdc: Decimal, recipient: str, refund_to: str) -> dict:
        """Create a live quote (dry=false) and return the deposit details."""
        base_units = int((amount_usdc * (10 ** ORIGIN_DECIMALS)).to_integral_value())
        body = {
            "dry": False,
            "swapType": "EXACT_INPUT",
            "slippageTolerance": 100,  # 1%
            "originAsset": ORIGIN_ASSET,
            "depositType": "ORIGIN_CHAIN",
            "destinationAsset": DEST_ASSET,
            "amount": str(base_units),
            "refundTo": refund_to,
            "refundType": "ORIGIN_CHAIN",
            "recipient": recipient,
            "recipientType": "DESTINATION_CHAIN",
            "deadline": _iso(datetime.now(timezone.utc) + timedelta(hours=1)),
            "depositMode": "SIMPLE",
        }
        data = await self._request("POST", "/v0/quote", json=body)
        quote = data.get("quote") or {}
        deposit = quote.get("depositAddress")
        if not deposit:
            raise BridgeError("Bridge did not return a deposit address")
        return {
            "deposit_address": deposit,
            "deposit_memo": quote.get("depositMemo"),
            "amount_in_formatted": quote.get("amountInFormatted"),
            "amount_in_usd": quote.get("amountInUsd"),
            "amount_out_formatted": quote.get("amountOutFormatted"),
            "amount_out_usd": quote.get("amountOutUsd"),
            "min_amount_out": quote.get("minAmountOut"),
            "time_estimate": quote.get("timeEstimate"),
            "deadline": quote.get("deadline"),
            "correlation_id": data.get("correlationId"),
        }

    async def get_status(self, deposit_address: str, deposit_memo: str | None = None) -> dict:
        params = {"depositAddress": deposit_address}
        if deposit_memo:
            params["depositMemo"] = deposit_memo
        data = await self._request("GET", "/v0/status", params=params)
        status = data.get("status", "UNKNOWN")
        swap_details = data.get("swapDetails") or {}
        return {"status": status, "swap_details": swap_details, "raw": data}
