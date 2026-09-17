"""Backend tests for USDC->Starknet Telegram bridge bot.

Covers:
- Health endpoints (/api/bot-info, /api/stats)
- Webhook security
- /bridge conversation via webhook (address save + swap insert with sid)
- /invoice bug fix (short sid callback_data, no Button_data_invalid)
- Cancel callback (cxl:<sid>) sets swap to CANCELLED
"""
import os
import time
import json
import subprocess
import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/") if os.environ.get("REACT_APP_BACKEND_URL") else "https://cross-chain-usdc-1.preview.emergentagent.com"
SECRET = "sn_bridge_7f3a9c21e8"
WEBHOOK = f"{BASE_URL}/api/telegram/webhook/{SECRET}"
CHAT_ID = 900900900
SN_ADDR = "0x04d9c8c2f2e6b6d3a4c5b6a7f8e9d0c1b2a3948576f8e9d0c1b2a3d4e5f60718"
BASE_ADDR = "0x1234567890123456789012345678901234567890"

mongo = MongoClient("mongodb://localhost:27017")
db = mongo["test_database"]


@pytest.fixture(scope="module", autouse=True)
def cleanup():
    db.users.delete_one({"_id": CHAT_ID})
    db.swaps.delete_many({"chat_id": CHAT_ID})
    yield
    db.users.delete_one({"_id": CHAT_ID})
    db.swaps.delete_many({"chat_id": CHAT_ID})


def _post(update):
    return requests.post(WEBHOOK, json=update, timeout=30)


def _msg(update_id, msg_id, text, is_command=False):
    m = {
        "update_id": update_id,
        "message": {
            "message_id": msg_id,
            "date": int(time.time()),
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "QA"},
            "text": text,
        },
    }
    if is_command:
        m["message"]["entities"] = [{"offset": 0, "length": len(text.split()[0]), "type": "bot_command"}]
    return m


# ---------- Basic endpoint tests ----------
class TestEndpoints:
    def test_bot_info(self):
        r = requests.get(f"{BASE_URL}/api/bot-info", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data.get("username") == "swaswabotbot", f"got {data}"

    def test_stats(self):
        r = requests.get(f"{BASE_URL}/api/stats", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert "total_swaps" in data
        assert "completed" in data
        assert isinstance(data["total_swaps"], int)


# ---------- Webhook security ----------
class TestWebhookSecurity:
    def test_wrong_secret_returns_403(self):
        bad = f"{BASE_URL}/api/telegram/webhook/wrong_secret"
        r = requests.post(bad, json={"update_id": 1}, timeout=15)
        assert r.status_code == 403

    def test_correct_secret_returns_ok(self):
        # Send a benign non-command message
        r = _post(_msg(600000, 1, "hello"))
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}


# ---------- Full bridge conversation ----------
class TestBridgeConversation:
    def test_full_bridge_flow_saves_addresses_and_creates_swap(self):
        # /bridge
        r = _post(_msg(700001, 10, "/bridge", is_command=True))
        assert r.status_code == 200, r.text
        time.sleep(1.5)

        # Send Starknet address
        r = _post(_msg(700002, 11, SN_ADDR))
        assert r.status_code == 200, r.text
        time.sleep(1.5)

        # Send Base address
        r = _post(_msg(700003, 12, BASE_ADDR))
        assert r.status_code == 200, r.text
        time.sleep(1.5)

        # Send amount (real NEAR quote may take a few seconds)
        r = _post(_msg(700004, 13, "2"))
        assert r.status_code == 200, r.text
        time.sleep(4)

        # Verify user addresses saved
        user = db.users.find_one({"_id": CHAT_ID})
        assert user is not None, "user not saved"
        assert SN_ADDR in (user.get("starknet") or []), f"starknet not saved: {user}"
        assert BASE_ADDR in (user.get("base") or []), f"base not saved: {user}"

        # Verify swap created
        swap = db.swaps.find_one({"chat_id": CHAT_ID, "is_invoice": {"$ne": True}})
        assert swap is not None, "swap not created"
        assert swap.get("sid"), "swap has no sid"
        assert len(swap["sid"]) <= 16, f"sid too long: {swap['sid']}"
        assert swap.get("status") == "PENDING_DEPOSIT"
        assert swap.get("amount_in") == "2"
        assert swap.get("recipient") == SN_ADDR
        assert swap.get("refund") == BASE_ADDR


# ---------- Invoice bug fix + cancel ----------
class TestInvoiceAndCancel:
    def test_invoice_creates_swap_with_sid_no_button_data_invalid(self):
        # Ensure user has starknet addr saved (from prior test or seed)
        db.users.update_one(
            {"_id": CHAT_ID},
            {"$set": {"starknet": [SN_ADDR], "base": [BASE_ADDR]}},
            upsert=True,
        )
        # Truncate log offset marker by remembering current end of log
        log_path = "/var/log/supervisor/backend.err.log"
        start_size = 0
        try:
            start_size = os.path.getsize(log_path)
        except OSError:
            pass

        r = _post(_msg(700100, 100, "/invoice 5", is_command=True))
        assert r.status_code == 200, r.text
        time.sleep(5)  # let NEAR quote and send

        # Verify invoice swap doc
        inv = db.swaps.find_one({"chat_id": CHAT_ID, "is_invoice": True})
        assert inv is not None, "invoice swap not created"
        assert inv.get("sid"), "invoice has no sid"
        assert inv.get("status") == "PENDING_DEPOSIT"
        assert inv.get("is_invoice") is True

        # Check log for Button_data_invalid
        try:
            with open(log_path, "rb") as f:
                f.seek(start_size)
                new_logs = f.read().decode("utf-8", errors="ignore")
            assert "Button_data_invalid" not in new_logs, "FOUND Button_data_invalid in logs!"
        except FileNotFoundError:
            pytest.skip("supervisor backend log not accessible")

    def test_cancel_callback_updates_status_to_cancelled(self):
        # Find an invoice with PENDING_DEPOSIT
        inv = db.swaps.find_one({"chat_id": CHAT_ID, "is_invoice": True, "status": "PENDING_DEPOSIT"})
        assert inv is not None, "no pending invoice to cancel"
        sid = inv["sid"]

        cq = {
            "update_id": 700200,
            "callback_query": {
                "id": "cq_test_1",
                "from": {"id": CHAT_ID, "is_bot": False, "first_name": "QA"},
                "chat_instance": "test_chat_instance",
                "message": {
                    "message_id": 500,
                    "date": int(time.time()),
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "from": {"id": 1, "is_bot": True, "first_name": "bot"},
                    "text": "invoice card",
                },
                "data": f"cxl:{sid}",
            },
        }
        r = requests.post(WEBHOOK, json=cq, timeout=30)
        assert r.status_code == 200, r.text
        time.sleep(2)

        updated = db.swaps.find_one({"sid": sid})
        assert updated is not None
        assert updated["status"] == "CANCELLED", f"status is {updated['status']}"
