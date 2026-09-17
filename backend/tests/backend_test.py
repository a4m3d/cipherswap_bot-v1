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
        # Enter universal via start callback
        assert d.text("/start", is_command=True).status_code == 200
        time.sleep(0.8)
        assert d.cb("start:uni").status_code == 200
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


# ---------- 12. Back navigation (nav:*) ----------
class TestBackNavigation:
    CHAT = 900901010

    def setup_method(self):
        _clear_chat(self.CHAT)

    def teardown_method(self):
        _clear_chat(self.CHAT)

    def test_back_from_src_coin_to_src_net(self):
        d = _Driver(self.CHAT)
        assert d.text("/start", is_command=True).status_code == 200
        time.sleep(0.6)
        assert d.cb("start:uni").status_code == 200
        time.sleep(0.4)
        assert d.cb("usn:base").status_code == 200  # -> src_coin
        time.sleep(0.4)
        # Back to source-network
        assert d.cb("nav:srcnet").status_code == 200
        time.sleep(0.4)
        # Should still be in conversation — pick another src net
        assert d.cb("usn:eth").status_code == 200
        time.sleep(0.4)
        # And a coin to keep advancing (this asserts state didn't blow up)
        r = d.cb("usc:USDC")
        assert r.status_code == 200 and r.json() == {"ok": True}


# ---------- 13. menu:open auto-recovery ----------
class TestMenuOpenRecovery:
    CHAT = 900901011

    def setup_method(self):
        _clear_chat(self.CHAT)

    def teardown_method(self):
        _clear_chat(self.CHAT)

    def test_menu_open_resets_conversation(self):
        d = _Driver(self.CHAT)
        assert d.text("/start", is_command=True).status_code == 200
        time.sleep(0.5)
        assert d.cb("start:uni").status_code == 200
        time.sleep(0.4)
        assert d.cb("usn:base").status_code == 200
        time.sleep(0.4)
        # Auto-recovery: tap Continue → resets and reopens menu
        r = d.cb("menu:open")
        assert r.status_code == 200 and r.json() == {"ok": True}
        time.sleep(0.4)
        # And we can start a fresh flow again
        assert d.cb("start:uni").status_code == 200


# ---------- 14. NL ambiguous (no networks) ----------
class TestNLAmbiguous:
    CHAT = 900901012

    def setup_method(self):
        _clear_chat(self.CHAT)

    def teardown_method(self):
        _clear_chat(self.CHAT)

    def test_ambiguous_returns_clarification_no_swap(self):
        d = _Driver(self.CHAT)
        offs = _log_offset()
        # USDC/USDT both exist on many chains -> ambiguous
        r = d.text("swap 5 USDC to USDT")
        assert r.status_code == 200
        time.sleep(2.0)
        # Nothing should be created
        assert db.swaps.find_one({"chat_id": self.CHAT}) is None
        # And no unhandled exception
        logs = _read_log_since(offs)
        for bad in ("Application shutting down", "unhandled exception"):
            assert bad not in logs


# ---------- 15. Amount preset via amt:* ----------
class TestAmountPreset:
    CHAT = 900901013

    def setup_method(self):
        _clear_chat(self.CHAT)

    def teardown_method(self):
        _clear_chat(self.CHAT)

    def test_amt_preset_advances(self):
        """A crowd amount (100) via amt:* preset skips blend and goes to recipient."""
        d = _Driver(self.CHAT)
        assert d.cb("start:uni").status_code == 200  # entry_point works without /start
        time.sleep(0.4)
        assert d.cb("usn:base").status_code == 200
        time.sleep(0.4)
        assert d.cb("usc:USDC").status_code == 200
        time.sleep(0.4)
        assert d.cb("udn:bsc").status_code == 200
        time.sleep(0.4)
        assert d.cb("udc:USDT").status_code == 200
        time.sleep(0.4)
        # Preset 100 is a CROWD amount -> should NOT show blend, jumps to recipient
        r = d.cb("amt:100")
        assert r.status_code == 200 and r.json() == {"ok": True}
        time.sleep(0.4)
        # Recipient text should now be accepted (proves we advanced past amount+blend)
        r2 = d.text(RECIPIENT)
        assert r2.status_code == 200


# ---------- 16. Favorites: savefav + fav:0 ----------
class TestFavorites:
    CHAT = 900901014

    def setup_method(self):
        _clear_chat(self.CHAT)
        # Seed a completed swap doc so savefav can find a route
        db.swaps.insert_one({
            "sid": "favseed",
            "chat_id": self.CHAT,
            "status": "SUCCESS",
            "src_sym": "USDC", "src_net": "base",
            "dst_sym": "USDT", "dst_net": "bsc",
            "amount_in": "1500",
            "route": {
                "origin_net": "base", "src_sym": "USDC",
                "origin_asset": "nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near",
                "origin_decimals": 6,
                "origin_contract": "0x833589FCd6eDb6E08f4c7C32D4f71b54bdA02913",
                "dest_net": "bsc", "dst_sym": "USDT",
                "dest_asset": "nep141:bsc-0x55d398326f99059ff775485246999027b3197955.omft.near",
            },
        })

    def teardown_method(self):
        _clear_chat(self.CHAT)

    def test_savefav_then_fav_starts_prefilled_flow(self):
        d = _Driver(self.CHAT)
        # Save favorite from the seeded sid
        assert d.cb("savefav:favseed").status_code == 200
        time.sleep(0.8)
        user = db.users.find_one({"_id": self.CHAT})
        assert user is not None
        favs = user.get("favorites") or []
        assert len(favs) == 1
        assert favs[0]["route"]["src_sym"] == "USDC"
        assert favs[0]["route"]["origin_net"] == "base"
        assert favs[0]["route"]["dst_sym"] == "USDT"
        assert favs[0]["route"]["dest_net"] == "bsc"

        # Now tap fav:0 - should start pre-filled flow (advances to amount)
        r = d.cb("fav:0")
        assert r.status_code == 200 and r.json() == {"ok": True}
        time.sleep(0.4)
        # Typing an amount should be accepted at amount step
        assert d.text("1500").status_code == 200


# ---------- 17. Repeat / Reverse from finished swap ----------
class TestRepeatReverse:
    CHAT = 900901015

    def setup_method(self):
        _clear_chat(self.CHAT)
        db.swaps.insert_one({
            "sid": "rptseed",
            "chat_id": self.CHAT,
            "status": "SUCCESS",
            "src_sym": "USDC", "src_net": "base",
            "dst_sym": "USDT", "dst_net": "bsc",
            "amount_in": "1500",
            "route": {
                "origin_net": "base", "src_sym": "USDC",
                "origin_asset": "nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near",
                "origin_decimals": 6,
                "origin_contract": "0x833589FCd6eDb6E08f4c7C32D4f71b54bdA02913",
                "dest_net": "bsc", "dst_sym": "USDT",
                "dest_asset": "nep141:bsc-0x55d398326f99059ff775485246999027b3197955.omft.near",
            },
        })

    def teardown_method(self):
        _clear_chat(self.CHAT)

    def test_rpt_starts_flow(self):
        d = _Driver(self.CHAT)
        r = d.cb("rpt:rptseed")
        assert r.status_code == 200 and r.json() == {"ok": True}
        time.sleep(0.4)
        # Amount step should accept a number
        assert d.text("1500").status_code == 200

    def test_rev_starts_flow(self):
        d = _Driver(self.CHAT)
        r = d.cb("rev:rptseed")
        assert r.status_code == 200 and r.json() == {"ok": True}
        time.sleep(0.4)
        # Amount step should accept a number
        assert d.text("1500").status_code == 200


# ---------- 18. Re-quote on expiry (rq:*) ----------
class TestRequote:
    CHAT = 900901016

    def setup_method(self):
        _clear_chat(self.CHAT)

    def teardown_method(self):
        _clear_chat(self.CHAT)

    def test_rq_cancels_old_creates_new(self):
        # First create a real swap via NL
        d = _Driver(self.CHAT)
        assert d.text("bridge 1500 usdc on base to usdt on bsc").status_code == 200
        time.sleep(1.5)
        assert d.text(RECIPIENT).status_code == 200
        time.sleep(0.8)
        assert d.text(REFUND).status_code == 200
        time.sleep(0.8)
        assert d.cb("prv:go").status_code == 200

        deadline = time.time() + 25
        old = None
        while time.time() < deadline:
            old = db.swaps.find_one({"chat_id": self.CHAT, "status": "PENDING_DEPOSIT"})
            if old:
                break
            time.sleep(1.0)
        assert old is not None, "no original swap created"
        old_sid = old["sid"]

        # Re-quote
        assert d.cb(f"rq:{old_sid}").status_code == 200
        # Wait for old to be cancelled + new to appear
        deadline = time.time() + 25
        new_swap = None
        while time.time() < deadline:
            after_old = db.swaps.find_one({"sid": old_sid})
            new_swap = db.swaps.find_one(
                {"chat_id": self.CHAT, "status": "PENDING_DEPOSIT",
                 "sid": {"$ne": old_sid}})
            if after_old and after_old.get("status") == "CANCELLED" and new_swap:
                break
            time.sleep(1.0)
        after_old = db.swaps.find_one({"sid": old_sid})
        assert after_old["status"] == "CANCELLED"
        assert new_swap is not None, "no fresh swap after re-quote"
        assert new_swap["amount_in"] == "1500"
        assert new_swap["recipient"] == RECIPIENT
        assert new_swap["refund"] == REFUND
        assert new_swap.get("deposit_address")


# ---------- 19. near_client.get_status tx-hash parsing (unit) ----------
class TestGetStatusTxHashes:
    def test_parses_swap_details_tx_hashes(self, monkeypatch):
        import asyncio
        from near_client import NearBridgeClient

        canned = {
            "status": "SUCCESS",
            "swapDetails": {
                "amountOutFormatted": "1499.10",
                "amountOutUsd": "1499.10",
                "originChainTxHashes": [
                    {"hash": "0xabc123", "explorerUrl": "https://basescan.org/tx/0xabc123"}
                ],
                "destinationChainTxHashes": [
                    {"hash": "0xdef456", "explorerUrl": "https://bscscan.com/tx/0xdef456"}
                ],
                "refundReason": None,
            },
            "refundReason": None,
        }

        async def fake_request(self, method, path, **kw):
            assert method == "GET" and path == "/v0/status"
            return canned

        monkeypatch.setattr(NearBridgeClient, "_request", fake_request)
        client = NearBridgeClient()
        res = asyncio.get_event_loop().run_until_complete(
            client.get_status("0xdeposit", None))
        assert res["status"] == "SUCCESS"
        assert res["origin_tx"] == "0xabc123"
        assert res["origin_tx_url"] == "https://basescan.org/tx/0xabc123"
        assert res["dest_tx"] == "0xdef456"
        assert res["dest_tx_url"] == "https://bscscan.com/tx/0xdef456"
        assert res["amount_out_formatted"] == "1499.10"
        assert res["refund_reason"] is None

    def test_missing_tx_hashes_return_none(self, monkeypatch):
        import asyncio
        from near_client import NearBridgeClient

        canned = {"status": "PROCESSING", "swapDetails": {}}

        async def fake_request(self, method, path, **kw):
            return canned

        monkeypatch.setattr(NearBridgeClient, "_request", fake_request)
        client = NearBridgeClient()
        res = asyncio.get_event_loop().run_until_complete(
            client.get_status("0xdeposit", None))
        assert res["status"] == "PROCESSING"
        assert res["origin_tx"] is None and res["origin_tx_url"] is None
        assert res["dest_tx"] is None and res["dest_tx_url"] is None

