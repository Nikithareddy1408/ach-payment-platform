"""Requirement R13: ACH returns. A COMPLETED payment can be marked RETURNED with an
R01-R99 return code; the code is recorded in the audit trail and a payment.returned webhook is sent."""
import psycopg
import pytest

from app.auth import create_customer_with_api_key
from app.domain import STATUSES, can_transition
from tests.harness import assert_valid_chain, new_payment, wait_for


def return_payment(h, payment_id, body=None, api_key="default", headers=None):
    return h.request("POST", f"/v1/payments/{payment_id}/returns",
                     json_body={"returnCode": "R01"} if body is None else body, api_key=api_key, headers=headers)


def completed_payment(h, **overrides) -> str:
    h.start()
    pid = h.submit(new_payment(**overrides)).json()["id"]
    assert h.wait_for_final(pid)["status"] == "COMPLETED"
    return pid


class TestReturnRules:
    def test_completed_to_returned_is_the_only_new_transition(self):
        assert "RETURNED" in STATUSES
        assert can_transition("COMPLETED", "RETURNED")
        for status in STATUSES:
            assert not can_transition("RETURNED", status), f"RETURNED -> {status}"
            if status != "COMPLETED":
                assert not can_transition(status, "RETURNED"), f"{status} -> RETURNED"

    def test_database_allows_completed_to_returned_and_refuses_other_returns(self, h):
        pid = h.submit(new_payment()).json()["id"]
        with pytest.raises(psycopg.errors.CheckViolation, match="illegal payment status transition PENDING -> RETURNED"):
            h.query("UPDATE payments SET status = 'RETURNED' WHERE id = %s", (pid,))

        done = completed_payment(h)
        h.query("UPDATE payments SET status = 'RETURNED' WHERE id = %s", (done,))
        with pytest.raises(psycopg.errors.CheckViolation, match="illegal payment status transition RETURNED -> COMPLETED"):
            h.query("UPDATE payments SET status = 'COMPLETED' WHERE id = %s", (done,))


class TestReturnEndpoint:
    def test_returns_a_completed_payment(self, h):
        pid = completed_payment(h)
        completed_at = h.request("GET", f"/v1/payments/{pid}").json()["completedAt"]
        res = return_payment(h, pid, {"returnCode": "R01"})
        assert res.status_code == 200, res.text
        assert res.json()["id"] == pid and res.json()["status"] == "RETURNED"
        current = h.request("GET", f"/v1/payments/{pid}").json()
        assert current["status"] == "RETURNED" and current["completedAt"] == completed_at
        assert [p["id"] for p in h.request("GET", "/v1/payments?status=RETURNED").json()["data"]] == [pid]

    def test_payment_that_is_not_completed_gives_409_and_nothing_changes(self, h):
        pending = h.submit(new_payment()).json()["id"]
        res = return_payment(h, pending)
        assert res.status_code == 409 and res.json()["error"]["code"] == "PAYMENT_NOT_RETURNABLE"
        assert h.request("GET", f"/v1/payments/{pending}").json()["status"] == "PENDING"
        assert [e["to"] for e in h.events(pending)] == ["PENDING"]

    def test_failed_payment_gives_409(self, h):
        h.start()
        pid = h.submit(new_payment(destinationAccount="EXT-CLOSED-01")).json()["id"]
        assert h.wait_for_final(pid)["status"] == "FAILED"
        res = return_payment(h, pid)
        assert res.status_code == 409 and res.json()["error"]["code"] == "PAYMENT_NOT_RETURNABLE"
        assert h.request("GET", f"/v1/payments/{pid}").json()["status"] == "FAILED"

    def test_returning_twice_gives_409(self, h):
        pid = completed_payment(h)
        assert return_payment(h, pid, {"returnCode": "R02"}).status_code == 200
        second = return_payment(h, pid, {"returnCode": "R03"})
        assert second.status_code == 409 and second.json()["error"]["code"] == "PAYMENT_NOT_RETURNABLE"
        assert [e["to"] for e in h.events(pid)].count("RETURNED") == 1

    @pytest.mark.parametrize("code", ["R01", "R10", "R99"])
    def test_accepts_codes_r01_to_r99(self, h, code):
        pid = completed_payment(h)
        assert return_payment(h, pid, {"returnCode": code}).status_code == 200

    @pytest.mark.parametrize("body", [
        {"returnCode": "R00"}, {"returnCode": "R100"}, {"returnCode": "r01"}, {"returnCode": "X01"},
        {"returnCode": "R1"}, {"returnCode": "R01 "}, {"returnCode": 1}, {}, {"returnCode": "R01", "extra": 1},
    ])
    def test_invalid_return_codes_are_rejected_and_nothing_changes(self, h, body):
        pid = completed_payment(h)
        res = return_payment(h, pid, body)
        assert res.status_code == 400 and res.json()["error"]["code"] == "VALIDATION_ERROR", res.text
        assert h.request("GET", f"/v1/payments/{pid}").json()["status"] == "COMPLETED"

    def test_unknown_or_other_customers_payment_is_404(self, h):
        pid = completed_payment(h)
        assert return_payment(h, "pay_" + "0" * 32).status_code == 404
        other_key = create_customer_with_api_key(h.platform.pool, "C99999", "Other Corp")["api_key"]
        res = return_payment(h, pid, api_key=other_key)
        assert res.status_code == 404 and res.json()["error"]["code"] == "PAYMENT_NOT_FOUND"
        assert h.request("GET", f"/v1/payments/{pid}").json()["status"] == "COMPLETED"

    def test_requires_authentication(self, h):
        pid = completed_payment(h)
        assert return_payment(h, pid, api_key=None).status_code == 401


class TestReturnAuditAndWebhook:
    def test_return_code_is_recorded_in_the_audit_trail(self, h):
        pid = completed_payment(h)
        res = return_payment(h, pid, {"returnCode": "R07"}, headers={"X-Request-Id": "req-return-test-1"})
        assert res.status_code == 200
        events = h.events(pid)
        assert_valid_chain(events, "RETURNED")
        last = events[-1]
        assert last["from"] == "COMPLETED" and last["to"] == "RETURNED"
        assert last["metadata"] == {"returnCode": "R07"}
        assert "R07" in last["reason"]
        assert last["actor"] == "api" and last["requestId"] == "req-return-test-1"

    def test_payment_returned_webhook_is_sent(self, h, receivers):
        r = receivers()
        h.request("POST", "/v1/webhook-endpoints", json_body={"url": r.url})
        pid = completed_payment(h)
        wait_for(lambda: len(r.received) == 3, what="lifecycle webhooks")
        assert return_payment(h, pid, {"returnCode": "R01"}).status_code == 200
        wait_for(lambda: len(r.received) == 4, what="payment.returned webhook")
        hook = r.received[-1]["json"]
        assert [w["json"]["type"] for w in r.received] == ["payment.pending", "payment.processing", "payment.completed", "payment.returned"]
        assert hook["data"]["previousStatus"] == "COMPLETED" and "R01" in hook["data"]["reason"]
        assert hook["data"]["payment"]["id"] == pid and hook["data"]["payment"]["status"] == "RETURNED"
        assert "VA10001" not in r.received[-1]["raw"]
