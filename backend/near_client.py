"""NEAR Intents (1-Click) client + token catalog for universal any-to-any swaps."""
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

BASE_URL = os.environ.get("NEAR_INTENTS_BASE", "https://1click.chaindefuser.com")
JWT = os.environ.get("NEAR_INTENTS_JWT", "").strip()

TERMINAL_STATUSES = {"SUCCESS", "REFUNDED", "FAILED"}

# Classic route (Base USDC -> Starknet STRK) kept for the original flow.
BASE_USDC_ASSET = "nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near"
STRK_ASSET = "nep141:starknet.omft.near"

# Friendly network names
NETWORK_NAMES = {
    "base": "Base", "eth": "Ethereum", "arb": "Arbitrum", "op": "Optimism",
    "bsc": "BNB Chain", "pol": "Polygon", "avax": "Avalanche", "sol": "Solana",
    "starknet": "Starknet", "near": "NEAR", "btc": "Bitcoin", "doge": "Dogecoin",
    "ltc": "Litecoin", "xrp": "XRP", "ton": "TON", "tron": "Tron", "sui": "Sui",
    "aptos": "Aptos", "zec": "Zcash", "gnosis": "Gnosis", "scroll": "Scroll",
    "bch": "Bitcoin Cash", "dash": "Dash", "stellar": "Stellar", "cardano": "Cardano",
    "bera": "Berachain", "monad": "Monad", "xlayer": "X Layer", "abs": "Abstract",
    "plasma": "Plasma", "hypercore": "Hyperliquid", "movement": "Movement",
    "aleo": "Aleo", "fogo": "Fogo", "adi": "Adi", "pol_zkevm": "Polygon zkEVM",
}


class BridgeError(Exception):
    pass


def _headers():
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if JWT:
        h["Authorization"] = f"Bearer {JWT}"
    return h


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def net_name(code):
    return NETWORK_NAMES.get(code, (code or "").upper())


class Catalog:
    """In-memory cached token catalog from /v0/tokens."""
    def __init__(self):
        self._tokens = []
        self._ts = 0

    async def load(self, force=False):
        if self._tokens and not force and (time.time() - self._ts) < 300:
            return self._tokens
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=25) as c:
            r = await c.get("/v0/tokens", headers=_headers())
        r.raise_for_status()
        self._tokens = r.json()
        self._ts = time.time()
        return self._tokens

    def networks(self):
        return sorted(set(t.get("blockchain") for t in self._tokens if t.get("blockchain")))

    def coins_on(self, network):
        seen = {}
        for t in self._tokens:
            if str(t.get("blockchain", "")).lower() == network.lower():
                seen.setdefault(t.get("symbol"), t)
        return list(seen.values())

    def find(self, symbol, network):
        symu = symbol.upper()
        for t in self._tokens:
            if t.get("symbol", "").upper() == symu and str(t.get("blockchain", "")).lower() == network.lower():
                return t
        return None

    def find_symbol(self, symbol):
        """All tokens matching a symbol across networks."""
        symu = symbol.upper()
        return [t for t in self._tokens if t.get("symbol", "").upper() == symu]

    def price(self, symbol, network):
        t = self.find(symbol, network)
        return t.get("price") if t else None


class NearBridgeClient:
    def __init__(self):
        self.origin_symbol = "USDC"
        self.dest_symbol = "STRK"
        self.catalog = Catalog()

    async def _request(self, method, path, **kwargs):
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as c:
            r = await c.request(method, path, headers=_headers(), **kwargs)
        if r.status_code >= 400:
            raise BridgeError(f"Bridge API {r.status_code}: {r.text[:300]}")
        return r.json()

    async def quote(self, origin_asset, dest_asset, amount_human, origin_decimals,
                    recipient, refund, dry=False):
        base_units = int((Decimal(str(amount_human)) * (10 ** origin_decimals)).to_integral_value())
        body = {
            "dry": dry,
            "swapType": "EXACT_INPUT",
            "slippageTolerance": 100,
            "originAsset": origin_asset,
            "depositType": "ORIGIN_CHAIN",
            "destinationAsset": dest_asset,
            "amount": str(base_units),
            "refundTo": refund,
            "refundType": "ORIGIN_CHAIN",
            "recipient": recipient,
            "recipientType": "DESTINATION_CHAIN",
            "deadline": _iso(datetime.now(timezone.utc) + timedelta(hours=1)),
            "depositMode": "SIMPLE",
        }
        data = await self._request("POST", "/v0/quote", json=body)
        q = data.get("quote") or {}
        deposit = q.get("depositAddress")
        if not deposit and not dry:
            raise BridgeError("Bridge did not return a deposit address for this pair")
        return {
            "deposit_address": deposit,
            "deposit_memo": q.get("depositMemo"),
            "amount_in_formatted": q.get("amountInFormatted"),
            "amount_in_usd": q.get("amountInUsd"),
            "amount_out_formatted": q.get("amountOutFormatted"),
            "amount_out_usd": q.get("amountOutUsd"),
            "time_estimate": q.get("timeEstimate"),
            "deadline": q.get("deadline"),
            "correlation_id": data.get("correlationId"),
        }

    async def create_swap(self, amount_usdc, recipient, refund):
        """Classic Base USDC -> Starknet STRK."""
        return await self.quote(BASE_USDC_ASSET, STRK_ASSET, amount_usdc, 6, recipient, refund)

    async def get_status(self, deposit_address, deposit_memo=None):
        params = {"depositAddress": deposit_address}
        if deposit_memo:
            params["depositMemo"] = deposit_memo
        data = await self._request("GET", "/v0/status", params=params)
        sd = data.get("swapDetails") or {}

        def _first(key):
            arr = sd.get(key) or []
            if arr and isinstance(arr[0], dict):
                return arr[0].get("hash"), arr[0].get("explorerUrl")
            return None, None

        o_hash, o_url = _first("originChainTxHashes")
        d_hash, d_url = _first("destinationChainTxHashes")
        return {
            "status": data.get("status", "UNKNOWN"),
            "origin_tx": o_hash, "origin_tx_url": o_url,
            "dest_tx": d_hash, "dest_tx_url": d_url,
            "amount_out_formatted": sd.get("amountOutFormatted"),
            "amount_out_usd": sd.get("amountOutUsd"),
            "refund_reason": data.get("refundReason") or sd.get("refundReason"),
            "raw": data,
        }
