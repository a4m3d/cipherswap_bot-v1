"""Custodial hot-wallet helper for the 'pay once -> backend splits' mode (EVM origins).

Generates a fresh EVM wallet, watches its USDC/ERC20 balance, and dispatches ERC20
transfers to NEAR Intents deposit addresses. The private key is returned to the user
for recovery so funds can never get permanently stuck.
"""
from decimal import Decimal

from eth_account import Account
from web3 import Web3

Account.enable_unaudited_hdwallet_features()

# Public RPCs + chain ids for supported EVM origins
EVM_NETWORKS = {
    "base": {"rpc": "https://mainnet.base.org", "chain_id": 8453, "name": "Base"},
    "eth": {"rpc": "https://eth.llamarpc.com", "chain_id": 1, "name": "Ethereum"},
    "arb": {"rpc": "https://arb1.arbitrum.io/rpc", "chain_id": 42161, "name": "Arbitrum"},
    "op": {"rpc": "https://mainnet.optimism.io", "chain_id": 10, "name": "Optimism"},
    "bsc": {"rpc": "https://bsc-dataseed.binance.org", "chain_id": 56, "name": "BNB Chain"},
    "pol": {"rpc": "https://polygon-rpc.com", "chain_id": 137, "name": "Polygon"},
    "avax": {"rpc": "https://api.avax.network/ext/bc/C/rpc", "chain_id": 43114, "name": "Avalanche"},
    "gnosis": {"rpc": "https://rpc.gnosischain.com", "chain_id": 100, "name": "Gnosis"},
    "scroll": {"rpc": "https://rpc.scroll.io", "chain_id": 534352, "name": "Scroll"},
}

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
     "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
]


def supported(network: str) -> bool:
    return network in EVM_NETWORKS


EXPLORERS = {
    "base": "https://basescan.org/tx/",
    "eth": "https://etherscan.io/tx/",
    "arb": "https://arbiscan.io/tx/",
    "op": "https://optimistic.etherscan.io/tx/",
    "bsc": "https://bscscan.com/tx/",
    "pol": "https://polygonscan.com/tx/",
    "avax": "https://snowtrace.io/tx/",
    "gnosis": "https://gnosisscan.io/tx/",
    "scroll": "https://scrollscan.com/tx/",
}


def explorer_tx(network, txhash):
    base = EXPLORERS.get(network)
    if not base or not txhash:
        return None
    h = txhash if str(txhash).startswith("0x") else "0x" + str(txhash)
    return base + h


def new_wallet():
    acct = Account.create()
    return acct.address, acct.key.hex()


def _w3(network):
    rpc = EVM_NETWORKS[network]["rpc"]
    return Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))


def erc20_balance(network, token_addr, decimals, address) -> Decimal:
    w3 = _w3(network)
    c = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
    raw = c.functions.balanceOf(Web3.to_checksum_address(address)).call()
    return Decimal(raw) / (Decimal(10) ** decimals)


def native_balance(network, address) -> Decimal:
    w3 = _w3(network)
    return Decimal(w3.eth.get_balance(Web3.to_checksum_address(address))) / Decimal(10 ** 18)


def send_erc20(network, pk, token_addr, decimals, to_addr, amount_human) -> str:
    """Sign & broadcast an ERC20 transfer. Returns tx hash hex. Raises on failure."""
    w3 = _w3(network)
    acct = Account.from_key(pk)
    token = Web3.to_checksum_address(token_addr)
    to_addr = Web3.to_checksum_address(to_addr)
    c = w3.eth.contract(address=token, abi=ERC20_ABI)
    value = int((Decimal(str(amount_human)) * (Decimal(10) ** decimals)).to_integral_value())
    nonce = w3.eth.get_transaction_count(acct.address)
    chain_id = EVM_NETWORKS[network]["chain_id"]
    fn = c.functions.transfer(to_addr, value)
    tx = {"from": acct.address, "nonce": nonce, "chainId": chain_id}
    try:
        gas = fn.estimate_gas({"from": acct.address})
    except Exception:
        gas = 120000
    tx["gas"] = int(gas * 1.2)
    try:
        base_fee = w3.eth.get_block("latest").get("baseFeePerGas")
    except Exception:
        base_fee = None
    if base_fee:
        tx["maxPriorityFeePerGas"] = w3.to_wei(1, "gwei")
        tx["maxFeePerGas"] = int(base_fee * 2 + tx["maxPriorityFeePerGas"])
    else:
        tx["gasPrice"] = w3.eth.gas_price
    built = fn.build_transaction(tx)
    signed = acct.sign_transaction(built)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    return h.hex()
