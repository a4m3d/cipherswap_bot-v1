"""Rule-based natural-language parser for swap/bridge commands. Private: text never leaves the server."""
import re
from decimal import Decimal, InvalidOperation

VERBS = ("swap", "bridge", "convert", "exchange", "send")

# network aliases -> catalog blockchain code
NET_ALIASES = {
    "base": "base",
    "eth": "eth", "ethereum": "eth", "mainnet": "eth",
    "arb": "arb", "arbitrum": "arb",
    "op": "op", "optimism": "op",
    "bsc": "bsc", "bnb": "bsc", "binance": "bsc",
    "pol": "pol", "polygon": "pol", "matic": "pol",
    "avax": "avax", "avalanche": "avax",
    "sol": "sol", "solana": "sol",
    "starknet": "starknet", "strk": "starknet",
    "near": "near",
    "btc": "btc", "bitcoin": "btc",
    "doge": "doge", "dogecoin": "doge",
    "ltc": "ltc", "litecoin": "ltc",
    "xrp": "xrp", "ripple": "xrp",
    "ton": "ton", "tron": "tron", "trx": "tron",
    "sui": "sui", "aptos": "aptos", "apt": "aptos",
    "zec": "zec", "zcash": "zec",
    "gnosis": "gnosis", "scroll": "scroll", "bch": "bch", "dash": "dash",
    "stellar": "stellar", "xlm": "stellar", "cardano": "cardano", "ada": "cardano",
    "bera": "bera", "berachain": "bera", "monad": "monad", "xlayer": "xlayer",
    "abstract": "abs", "abs": "abs", "plasma": "plasma", "hyperliquid": "hypercore",
    "hypercore": "hypercore", "movement": "movement", "aleo": "aleo", "fogo": "fogo",
}

AMOUNT_RE = re.compile(r"(\d+(?:[.,]\d+)?)")


def _net(token):
    t = token.strip().lower()
    return NET_ALIASES.get(t)


def _extract_amount(text):
    m = AMOUNT_RE.search(text)
    if not m:
        return None, text
    raw = m.group(1).replace(",", ".")
    try:
        amt = Decimal(raw)
    except InvalidOperation:
        return None, text
    text = (text[:m.start()] + " " + text[m.end():]).strip()
    return amt, text


def _first_coin(words, catalog):
    """Return (symbol_token, network_code_or_None) scanning words for a known coin symbol."""
    for w in words:
        cw = w.strip().lower()
        if not cw or cw in ("on", "to", "the", "from"):
            continue
        matches = catalog.find_symbol(cw)
        if matches:
            return cw.upper()
    return None


def parse(text, catalog):
    """Return dict: {ok, amount, src_sym, src_net, dst_sym, dst_net, error, suggestions}."""
    low = " " + text.lower().strip() + " "
    if not any(f" {v} " in low or low.strip().startswith(v) for v in VERBS):
        return {"ok": False, "error": None}  # not a command; ignore silently

    body = text.lower()
    for v in VERBS:
        body = re.sub(rf"\b{v}\b", " ", body)
    body = body.strip()

    if " to " not in f" {body} ":
        return {"ok": False, "error": "Try: `swap 5 USDC on base to USDT on bsc`"}

    left, right = body.split(" to ", 1)

    amount, left = _extract_amount(left)

    # networks via 'on X'
    src_net = dst_net = None
    m = re.search(r"\bon\s+([a-z0-9]+)", left)
    if m:
        src_net = _net(m.group(1))
        left = left[:m.start()].strip()
    m = re.search(r"\bon\s+([a-z0-9]+)", right)
    if m:
        dst_net = _net(m.group(1))
        right = right[:m.start()].strip()

    src_sym = _first_coin(left.split(), catalog)
    dst_sym = _first_coin(right.split(), catalog)

    if not src_sym or not dst_sym:
        bad = right if not dst_sym else left
        return {"ok": False, "error": f"I couldn't recognise a coin in `{bad.strip() or '?'}`. Example: `bridge 5 USDC on base to USDT on bsc`"}

    # infer networks
    def resolve(sym, net):
        if net:
            t = catalog.find(sym, net)
            return (net, t)
        matches = catalog.find_symbol(sym)
        nets = sorted(set(str(t.get("blockchain")).lower() for t in matches))
        if len(nets) == 1:
            return (nets[0], catalog.find(sym, nets[0]))
        return (None, None)  # ambiguous

    if not src_net and dst_net:
        # if source omitted, don't assume dest; leave for menu unless unique
        pass
    src_net_r, src_tok = resolve(src_sym, src_net)
    # if dst network omitted: infer from dst coin uniqueness, else same-chain if available
    if not dst_net:
        dnets = sorted(set(str(t.get("blockchain")).lower() for t in catalog.find_symbol(dst_sym)))
        if len(dnets) == 1:
            dst_net = dnets[0]
        elif src_net_r and catalog.find(dst_sym, src_net_r):
            dst_net = src_net_r
    dst_net_r, dst_tok = resolve(dst_sym, dst_net)

    result = {
        "ok": True,
        "amount": amount,
        "src_sym": src_sym, "src_net": src_net_r,
        "dst_sym": dst_sym, "dst_net": dst_net_r,
        "src_tok": src_tok, "dst_tok": dst_tok,
        "error": None,
    }
    # ambiguity / availability messaging
    if src_net_r and not src_tok:
        result["ok"] = False
        result["error"] = f"{src_sym} isn't available on {src_net}."
    elif dst_net_r and not dst_tok:
        result["ok"] = False
        result["error"] = f"{dst_sym} isn't available on {dst_net or '?'}."
    elif not src_net_r:
        result["ok"] = False
        result["error"] = f"{src_sym} exists on several chains — tell me which, e.g. `on base`."
    elif not dst_net_r:
        result["ok"] = False
        result["error"] = f"{dst_sym} exists on several chains — tell me which, e.g. `on bsc`."
    return result
