"""Backend tests for CipherSwap Telegram bridge bot (2-mode rebuild).

Drives the bot via webhook POSTs (fake Telegram chats) and asserts against Mongo.
All bot sends fail with 'Chat not found' but are wrapped, so flow completes.
Uses amounts >= 1000 (NEAR temp minimum). Each test class uses its own chat_id.
"""
import os
import time
import pytest
import requests
from decimal import Decimal
from pymongo import MongoClient

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
SECRET = "sn_bridge_7f3a9c21e8"
WEBHOOK = f"{BASE_URL}/api/telegram/webhook/{SECRET}"

RECIPIENT = "0x1111111111111111111111111111111111111111"
REFUND = "0x2222222222222222222222222222222222222222"

mongo = MongoClient("mongodb://localhost:27017")
db = mongo["test_database"]

BACKEND_LOG = "/var/log/supervisor/backend.err.log"


def _log_offset():
    try:
        return os.path.getsize(BACKEND_LOG)
    except OSError:
        return 0


def _read_log_since(offset):
    try:
        with open(BACKEND_LOG, "rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _clear_chat(chat_id):
    db.users.delete_one({"_id": chat_id})
    db.swaps.delete_many({"chat_id": chat_id})
    db.custodial.delete_many({"chat_id": chat_id})


class _Driver:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.uid = chat_id * 10
        self.mid = 0

    def _next(self):
        self.uid += 1
        self.mid += 1
        return self.uid, self.mid

    def post(self, update, timeout=30):
        return requests.post(WEBHOOK, json=update, timeout=timeout)

    def text(self, text, is_command=False):
        uid, mid = self._next()
        m = {
            "update_id": uid,
            "message": {
                "message_id": mid,
                "date": int(time.time()),
                "chat": {"id": self.chat_id, "type": "private"},
                "from": {"id": self.chat_id, "is_bot": False, "first_name": "QA"},
                "text": text,
            },
        }
        if is_command:
            m["message"]["entities"] = [
                {"offset": 0, "length": len(text.split()[0]), "type": "bot_command"}
            ]
        return self.post(m)

    def cb(self, data):
        uid, mid = self._next()
        u = {
            "update_id": uid,
            "callback_query": {
                "id": f"cq_{uid}",
                "chat_instance": "111",
                "from": {"id": self.chat_id, "is_bot": False, "first_name": "QA"},
                "message": {
                    "message_id": mid,
                    "date": int(time.time()),
                    "chat": {"id": self.chat_id, "type": "private"},
                    "from": {"id": 1, "is_bot": True, "first_name": "bot"},
                    "text": "prompt",
                },
                "data": data,
            },
        }
        return self.post(u)


# ---------- 1. endpoints ----------
class TestEndpoints:
    def test_bot_info(self):
        r = requests.get(f"{BASE_URL}/api/bot-info", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data.get("username") == "swaswabotbot", data

    def test_stats(self):
        r = requests.get(f"{BASE_URL}/api/stats", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert "total_swaps" in data and isinstance(data["total_swaps"], int)


# ---------- 2. webhook security ----------
class TestWebhookSecurity:
    def test_wrong_secret(self):
        r = requests.post(
            f"{BASE_URL}/api/telegram/webhook/nope",
            json={"update_id": 1}, timeout=15,
        )
        assert r.status_code == 403

    def test_ok_secret(self):
        d = _Driver(900901000)
        r = d.text("hello")
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        _clear_chat(900901000)


# ---------- 3. /start does not crash ----------
class TestStartCommand:
    CHAT = 900901001

    def teardown_method(self):
        _clear_chat(self.CHAT)

    def test_start_returns_ok(self):
        d = _Driver(self.CHAT)
        r = d.text("/start", is_command=True)
        assert r.status_code == 200
        # No swap docs should be created just by /start
        time.sleep(1.0)
        assert db.swaps.find_one({"chat_id": self.CHAT}) is None


# ---------- 4. NL universal single swap + address book ----------
class TestNLSingleSwapAndAddressBook:
    CHAT = 900901002

    def setup_method(self):
        _clear_chat(self.CHAT)

    def teardown_method(self):
        _clear_chat(self.CHAT)

    def test_nl_bridge_creates_swap_and_saves_book(self):
        d = _Driver(self.CHAT)
        offs = _log_offset()

        # NL command prefills route + amount, jumps straight to recipient prompt
        assert d.text("bridge 1500 usdc on base to usdt on bsc").status_code == 200
        time.sleep(1.5)
        # Recipient (bsc EVM address)
        assert d.text(RECIPIENT).status_code == 200
        time.sleep(1.0)
        # Refund (base EVM address)
        assert d.text(REFUND).status_code == 200
        time.sleep(1.0)
        # Privacy: confirm defaults
        assert d.cb("prv:go").status_code == 200

        # Wait for NEAR quote (real API call)
        deadline = time.time() + 25
        swap = None
        while time.time() < deadline:
            swap = db.swaps.find_one({"chat_id": self.CHAT})
            if swap:
                break
            time.sleep(1.0)

        assert swap is not None, "no swap doc created after NL flow"
        assert swap["src_sym"] == "USDC"
        assert swap["src_net"] == "base"
        assert swap["dst_sym"] == "USDT"
        assert swap["dst_net"] == "bsc"
        assert swap["amount_in"] == "1500"
        assert swap["status"] == "PENDING_DEPOSIT"
        assert swap.get("deposit_address"), "no deposit_address on swap doc"
        assert swap["recipient"] == RECIPIENT
        assert swap["refund"] == REFUND

        # Address book saved under book.<net>
        user = db.users.find_one({"_id": self.CHAT})
        assert user is not None
        book = user.get("book") or {}
        assert RECIPIENT in (book.get("bsc") or []), f"recipient not in book.bsc: {book}"
        assert REFUND in (book.get("base") or []), f"refund not in book.base: {book}"

        # No hangs
        logs = _read_log_since(offs)
        for bad in ("Application shutting down", "unhandled exception"):
            assert bad not in logs, f"found {bad!r} in logs"


# ---------- 5. Address book reuse on 2nd swap ----------
class TestAddressBookReuse:
    CHAT = 900901003

    def setup_method(self):
        _clear_chat(self.CHAT)
        # Seed a saved bsc + base address in the book
        db.users.update_one(
            {"_id": self.CHAT},
            {"$set": {"book": {"bsc": [RECIPIENT], "base": [REFUND]}}},
            upsert=True,
        )

    def teardown_method(self):
        _clear_chat(self.CHAT)

    def test_second_swap_uses_saved_addresses(self):
        d = _Driver(self.CHAT)
        assert d.text("bridge 1500 usdc on base to usdt on bsc").status_code == 200
        time.sleep(1.5)
        # Now recipient should be a callback selection (rcp:0 -> saved index 0)
        assert d.cb("rcp:0").status_code == 200
        time.sleep(1.0)
        # Refund also from book
        assert d.cb("rfd:0").status_code == 200
        time.sleep(1.0)
        assert d.cb("prv:go").status_code == 200

        deadline = time.time() + 25
        swap = None
        while time.time() < deadline:
            swap = db.swaps.find_one({"chat_id": self.CHAT})
            if swap:
                break
            time.sleep(1.0)
        assert swap is not None, "no swap created via saved-addr callback path"
        assert swap["recipient"] == RECIPIENT
        assert swap["refund"] == REFUND


# ---------- 6. Guided menu flow ----------
class TestGuidedMenu:
    CHAT = 900901004

    def setup_method(self):
        _clear_chat(self.CHAT)

    def teardown_method(self):
        _clear_chat(self.CHAT)

    def test_menu_universal_flow(self):
        d = _Driver(self.CHAT)
        # Enter universal via mode callback
        assert d.text("/start", is_command=True).status_code == 200
        time.sleep(0.8)
        assert d.cb("mode:uni").status_code == 200
        time.sleep(0.8)
        assert d.cb("usn:base").status_code == 200
        time.sleep(0.6)
        assert d.cb("usc:USDC").status_code == 200
        time.sleep(0.6)
        assert d.cb("udn:bsc").status_code == 200
        time.sleep(0.6)
        assert d.cb("udc:USDT").status_code == 200
        time.sleep(0.6)
        assert d.text("1500").status_code == 200
        time.sleep(0.8)
        # 1500 triggers blend suggestion (not in CROWD_AMOUNTS); keep original
        assert d.cb("bl:keep").status_code == 200
        time.sleep(0.6)
        assert d.text(RECIPIENT).status_code == 200
        time.sleep(0.8)
        assert d.text(REFUND).status_code == 200
        time.sleep(0.8)
        assert d.cb("prv:go").status_code == 200

        deadline = time.time() + 25
        swap = None
        while time.time() < deadline:
            swap = db.swaps.find_one({"chat_id": self.CHAT})
            if swap:
                break
            time.sleep(1.0)
        assert swap is not None, "menu flow did not create swap"
        assert swap["src_sym"] == "USDC" and swap["src_net"] == "base"
        assert swap["dst_sym"] == "USDT" and swap["dst_net"] == "bsc"
        assert swap["amount_in"] == "1500"


# ---------- 7. Split (multi-address, non-custodial) ----------
class TestSplitMultiAddress:
    CHAT = 900901005

    def setup_method(self):
        _clear_chat(self.CHAT)

    def teardown_method(self):
        _clear_chat(self.CHAT)

    def test_split_creates_multiple_swaps_same_gid(self):
        d = _Driver(self.CHAT)
        assert d.text("bridge 3000 usdc on base to usdt on bsc").status_code == 200
        time.sleep(1.5)
        assert d.text(RECIPIENT).status_code == 200
        time.sleep(0.8)
        assert d.text(REFUND).status_code == 200
        time.sleep(0.8)
        # Toggle split once (0 -> 1 -> 2-3 chunks). style stays multi. Delays off.
        assert d.cb("prv:split").status_code == 200
        time.sleep(0.6)
        assert d.cb("prv:go").status_code == 200

        # Real NEAR quote per chunk (~8s each) — wait longer
        deadline = time.time() + 45
        swaps = []
        while time.time() < deadline:
            swaps = list(db.swaps.find({"chat_id": self.CHAT, "status": "PENDING_DEPOSIT"}))
            if len(swaps) >= 2:
                break
            time.sleep(1.5)

        assert len(swaps) >= 2, f"expected >=2 split swaps, got {len(swaps)}"
        gids = {s.get("gid") for s in swaps}
        assert len(gids) == 1 and None not in gids, f"chunks should share one gid, got {gids}"

        total = sum(Decimal(s["amount_in"]) for s in swaps)
        assert total == Decimal("3000"), f"chunks sum to {total}, expected 3000"

        # cxlp: cancel all remaining
        gid = swaps[0]["gid"]
        assert d.cb(f"cxlp:{gid}").status_code == 200
        time.sleep(1.5)
        after = list(db.swaps.find({"chat_id": self.CHAT, "gid": gid}))
        assert after
        for s in after:
            assert s["status"] == "CANCELLED", f"{s['sid']} status={s['status']}"


# ---------- 8. Zero-Trace + per-tx cancel ----------
class TestZeroTraceAndCancel:
    CHAT = 900901006

    def setup_method(self):
        _clear_chat(self.CHAT)

    def teardown_method(self):
        _clear_chat(self.CHAT)

    def test_zero_trace_and_cxl(self):
        d = _Driver(self.CHAT)
        assert d.text("bridge 1500 usdc on base to usdt on bsc").status_code == 200
        time.sleep(1.5)
        assert d.text(RECIPIENT).status_code == 200
        time.sleep(0.8)
        assert d.text(REFUND).status_code == 200
        time.sleep(0.8)
        assert d.cb("prv:zt").status_code == 200
        time.sleep(0.5)
        assert d.cb("prv:go").status_code == 200

        deadline = time.time() + 25
        zt = None
        while time.time() < deadline:
            zt = db.swaps.find_one({"chat_id": self.CHAT, "ephemeral": True})
            if zt:
                break
            time.sleep(1.0)
        assert zt is not None, "no ephemeral swap created"
        assert zt["status"] == "PENDING_DEPOSIT"

        # Per-tx cancel
        assert d.cb(f"cxl:{zt['sid']}").status_code == 200
        time.sleep(1.5)
        updated = db.swaps.find_one({"sid": zt["sid"]})
        assert updated["status"] == "CANCELLED"


# ---------- 9. Clear history ----------
class TestClearHistory:
    CHAT = 900901007

    def setup_method(self):
        _clear_chat(self.CHAT)
        db.users.insert_one({"_id": self.CHAT, "book": {"bsc": [RECIPIENT]}})
        db.swaps.insert_one({
            "sid": "seedxx", "chat_id": self.CHAT, "status": "PENDING_DEPOSIT",
            "src_sym": "USDC", "src_net": "base", "dst_sym": "USDT", "dst_net": "bsc",
            "amount_in": "1500",
        })
        db.custodial.insert_one({"chat_id": self.CHAT, "dispatched": False, "address": "0xdead"})

    def teardown_method(self):
        _clear_chat(self.CHAT)

    def test_clear_history_deletes_all_user_docs(self):
        d = _Driver(self.CHAT)
        assert d.cb("clr:ask").status_code == 200
        time.sleep(0.5)
        assert d.cb("clr:yes").status_code == 200
        time.sleep(1.0)
        assert db.users.find_one({"_id": self.CHAT}) is None
        assert db.swaps.find_one({"chat_id": self.CHAT}) is None
        assert db.custodial.find_one({"chat_id": self.CHAT}) is None


# ---------- 10. Custodial pay-once split creates a custodial doc ----------
class TestCustodialSplit:
    CHAT = 900901008

    def setup_method(self):
        _clear_chat(self.CHAT)

    def teardown_method(self):
        _clear_chat(self.CHAT)

    def test_custodial_doc_created(self):
        d = _Driver(self.CHAT)
        assert d.text("bridge 3000 usdc on base to usdt on bsc").status_code == 200
        time.sleep(1.5)
        assert d.text(RECIPIENT).status_code == 200
        time.sleep(0.8)
        assert d.text(REFUND).status_code == 200
        time.sleep(0.8)
        # split -> 1 (multi 2-3), style -> custodial
        assert d.cb("prv:split").status_code == 200
        time.sleep(0.4)
        assert d.cb("prv:style").status_code == 200
        time.sleep(0.4)
        assert d.cb("prv:go").status_code == 200
        time.sleep(3.0)

        cust = db.custodial.find_one({"chat_id": self.CHAT})
        assert cust is not None, "no custodial doc created"
        assert cust.get("address", "").startswith("0x")
        assert "pk" in cust and cust["pk"]
        chunks = cust.get("chunks") or []
        assert len(chunks) >= 2, f"expected >=2 chunks, got {chunks}"
        total = sum(Decimal(c) for c in chunks)
        assert total == Decimal("3000"), f"chunks sum={total}"
        # Ensure no swap docs were created for this chat (custodial waits for funding)
        assert db.swaps.find_one({"chat_id": self.CHAT}) is None


# ---------- 11. Under-min amount returns clean error, no swap ----------
class TestUnderMinAmount:
    CHAT = 900901009

    def setup_method(self):
        _clear_chat(self.CHAT)

    def teardown_method(self):
        _clear_chat(self.CHAT)

    def test_small_amount_no_swap_created(self):
        d = _Driver(self.CHAT)
        offs = _log_offset()
        assert d.text("bridge 5 usdc on base to usdt on bsc").status_code == 200
        time.sleep(1.5)
        assert d.text(RECIPIENT).status_code == 200
        time.sleep(0.8)
        assert d.text(REFUND).status_code == 200
        time.sleep(0.8)
        assert d.cb("prv:go").status_code == 200
        time.sleep(12)

        # No swap doc created since NEAR rejects with min-amount error
        swap = db.swaps.find_one({"chat_id": self.CHAT})
        assert swap is None, f"swap unexpectedly created for under-min amount: {swap}"

        logs = _read_log_since(offs)
        for bad in ("Application shutting down", "unhandled exception"):
            assert bad not in logs, f"found {bad!r} in logs"
