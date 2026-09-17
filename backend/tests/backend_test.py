"""Backend tests for USDC->Starknet Telegram bridge bot – Privacy Suite.

Each test class uses its OWN chat_id so pytest-xdist loadscope parallel workers
don't race on shared conversation state.
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

SN1 = "0x04d9c8c2f2e6b6d3a4c5b6a7f8e9d0c1b2a3948576f8e9d0c1b2a3d4e5f60718"
SN2 = "0x05ab00000000000000000000000000000000000000000000000000000000abcd"
BASE_ADDR = "0x1234567890123456789012345678901234567890"

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


def _seed_user(chat_id):
    db.users.update_one(
        {"_id": chat_id},
        {"$set": {"starknet": [SN1, SN2], "base": [BASE_ADDR], "rot_idx": 0}},
        upsert=True,
    )


def _clear_user(chat_id):
    db.users.delete_one({"_id": chat_id})
    db.swaps.delete_many({"chat_id": chat_id})


class _Driver:
    """Isolated update-id/msg-id counters per chat_id."""

    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.uid = chat_id * 10  # unique across classes
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
    CHAT = 900900801

    def test_wrong_secret(self):
        r = requests.post(f"{BASE_URL}/api/telegram/webhook/nope", json={"update_id": 1}, timeout=15)
        assert r.status_code == 403

    def test_ok_secret(self):
        d = _Driver(self.CHAT)
        r = d.text("hello")
        assert r.status_code == 200
        assert r.json() == {"ok": True}


# ---------- 3. /invoice removed ----------
class TestInvoiceRemoved:
    CHAT = 900900802

    def setup_method(self):
        _clear_user(self.CHAT)
        _seed_user(self.CHAT)

    def teardown_method(self):
        _clear_user(self.CHAT)

    def test_invoice_command_does_nothing(self):
        d = _Driver(self.CHAT)
        r = d.text("/invoice 10", is_command=True)
        assert r.status_code == 200
        time.sleep(2)
        inv = db.swaps.find_one({"chat_id": self.CHAT, "is_invoice": True})
        assert inv is None, f"invoice doc created after removal: {inv}"
        # Also no swap doc at all (no handler processed the command)
        any_swap = db.swaps.find_one({"chat_id": self.CHAT})
        assert any_swap is None, f"unexpected swap after /invoice: {any_swap}"


# ---------- 4. Split + rotation (also tests cxlp cancel-plan) ----------
class TestSplitRotationAndCancelPlan:
    CHAT = 900900803

    def setup_method(self):
        _clear_user(self.CHAT)
        _seed_user(self.CHAT)

    def teardown_method(self):
        _clear_user(self.CHAT)

    def test_split_flow_then_cancel_plan(self):
        d = _Driver(self.CHAT)
        offs = _log_offset()

        assert d.text("/bridge", is_command=True).status_code == 200
        time.sleep(1.0)
        assert d.cb("sn:rot").status_code == 200
        time.sleep(0.8)
        assert d.cb("bs:0").status_code == 200
        time.sleep(0.8)
        assert d.text("50").status_code == 200  # in CROWD_AMOUNTS → skips blend
        time.sleep(1.2)
        assert d.cb("prv:split").status_code == 200
        time.sleep(0.6)
        assert d.cb("prv:go").status_code == 200
        # Wait for real NEAR quotes for each chunk
        time.sleep(18)

        swaps = list(db.swaps.find({"chat_id": self.CHAT, "status": "PENDING_DEPOSIT"}))
        assert len(swaps) >= 2, f"expected >=2 split swaps, got {len(swaps)}"

        gids = {s.get("gid") for s in swaps}
        assert len(gids) == 1 and None not in gids, f"chunks should share one gid, got {gids}"
        gid = swaps[0]["gid"]

        total = sum(Decimal(s["amount_in"]) for s in swaps)
        assert total == Decimal("50"), f"chunks sum to {total}, expected 50"

        recips = {s["recipient"] for s in swaps}
        assert recips.issubset({SN1, SN2}), f"unexpected recipients: {recips}"
        assert len(recips) >= 2, f"rotation did not spread across addresses: {recips}"

        user = db.users.find_one({"_id": self.CHAT})
        assert (user or {}).get("rot_idx", 0) >= len(swaps)

        logs = _read_log_since(offs)
        for bad in ("Application shutting down", "unhandled exception"):
            assert bad not in logs, f"found {bad!r} in logs"

        # ---- cxlp: cancel-plan ----
        r = d.cb(f"cxlp:{gid}")
        assert r.status_code == 200
        time.sleep(1.5)
        after = list(db.swaps.find({"chat_id": self.CHAT, "gid": gid}))
        assert after
        for s in after:
            assert s["status"] == "CANCELLED", f"{s['sid']} status={s['status']}"


# ---------- 5. Zero-Trace + per-tx cancel ----------
class TestZeroTraceAndPerTxCancel:
    CHAT = 900900804

    def setup_method(self):
        _clear_user(self.CHAT)
        _seed_user(self.CHAT)

    def teardown_method(self):
        _clear_user(self.CHAT)

    def test_zero_trace_then_cxl_cancel(self):
        d = _Driver(self.CHAT)
        assert d.text("/bridge", is_command=True).status_code == 200
        time.sleep(1.0)
        assert d.cb("sn:0").status_code == 200
        time.sleep(0.6)
        assert d.cb("bs:0").status_code == 200
        time.sleep(0.6)
        assert d.text("50").status_code == 200
        time.sleep(1.0)
        assert d.cb("prv:zt").status_code == 200
        time.sleep(0.5)
        assert d.cb("prv:go").status_code == 200
        time.sleep(12)

        zt = db.swaps.find_one({"chat_id": self.CHAT, "ephemeral": True})
        assert zt is not None, "no ephemeral swap created"
        assert zt.get("status") == "PENDING_DEPOSIT"

        # per-tx cancel
        r = d.cb(f"cxl:{zt['sid']}")
        assert r.status_code == 200
        time.sleep(1.5)
        updated = db.swaps.find_one({"sid": zt["sid"]})
        assert updated["status"] == "CANCELLED"


# ---------- 6. Blend-In on odd amount ----------
class TestBlendIn:
    CHAT = 900900805

    def setup_method(self):
        _clear_user(self.CHAT)
        _seed_user(self.CHAT)

    def teardown_method(self):
        _clear_user(self.CHAT)

    def test_odd_amount_blend_keep(self):
        d = _Driver(self.CHAT)
        assert d.text("/bridge", is_command=True).status_code == 200
        time.sleep(1.0)
        assert d.cb("sn:0").status_code == 200
        time.sleep(0.5)
        assert d.cb("bs:0").status_code == 200
        time.sleep(0.5)
        assert d.text("37").status_code == 200  # not in CROWD_AMOUNTS → BR_BLEND
        time.sleep(1.0)
        assert d.cb("bl:keep").status_code == 200
        time.sleep(0.8)
        assert d.cb("prv:go").status_code == 200
        time.sleep(12)

        s = db.swaps.find_one({"chat_id": self.CHAT, "amount_in": "37"})
        assert s is not None, "blend-keep did not create swap with amount 37"
        assert s["status"] == "PENDING_DEPOSIT"
