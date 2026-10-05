"""PDF requirement: "Payment Submission API" (+ the security a real payment API needs)."""
import pytest

from app.auth import create_customer_with_api_key, revoke_api_key
from tests.harness import new_payment


def payment_count(h) -> int:
    return h.query("SELECT count(*) AS n FROM payments")[0]["n"]


def test_accepts_the_exact_example_from_the_pdf(h):
    res = h.submit({"customerId": "C12345", "sourceAccount": "VA10001", "destinationAccount": "EXT98765",
                    "amount": 250.00, "reference": "PMT-1001"}, "spec-example-key-0001")
    assert res.status_code == 202
    body = res.json()
    assert body["status"] == "PENDING" and body["amount"] == "250.00" and body["amountCents"] == 25000
    assert body["customerId"] == "C12345" and body["reference"] == "PMT-1001" and body["attempts"] == 0
    assert body["id"].startswith("pay_") and len(body["id"]) == 36
    assert res.headers["location"] == f"/v1/payments/{body['id']}"
    assert res.headers["x-request-id"].startswith("req_")


def test_never_exposes_full_account_numbers(h):
    body = h.submit(new_payment(sourceAccount="VA10009999", destinationAccount="EXT12345678")).json()
    assert body["sourceAccount"] == "****9999" and body["destinationAccount"] == "****5678"
    assert "VA10009999" not in str(body)


@pytest.mark.parametrize("amount, cents", [(19.99, 1999), ("0.10", 10), ("250", 25000), (0.01, 1), ("1000000.00", 100_000_000)])
def test_stores_amounts_exactly_in_cents(h, amount, cents):
    res = h.submit(new_payment(amount=amount))
    assert res.status_code == 202
    assert res.json()["amountCents"] == cents


@pytest.mark.parametrize("case, overrides, field", [
    ("missing customerId", {"customerId": None}, "customerId"),
    ("negative amount", {"amount": -5}, "amount"),
    ("zero amount", {"amount": 0}, "amount"),
    ("more than 2 decimals", {"amount": 10.123}, "amount"),
    ("amount as words", {"amount": "ten dollars"}, "amount"),
    ("boolean amount", {"amount": True}, "amount"),
    ("amount over $1,000,000", {"amount": 1000000.01}, "amount"),
    ("same source and destination", {"destinationAccount": "VA10001"}, "destinationAccount"),
    ("account number containing SQL", {"sourceAccount": "VA1'; DROP TABLE payments;--"}, "sourceAccount"),
    ("empty reference", {"reference": ""}, "reference"),
    ("unknown extra field (typo protection)", {"amout": 5}, "amout"),
])
def test_rejects_invalid_input_and_stores_nothing(h, case, overrides, field):
    payment = {k: v for k, v in new_payment(**overrides).items() if v is not None}
    before = payment_count(h)
    res = h.submit(payment, f"invalid-{case.replace(' ', '-')}-key")
    assert res.status_code == 400, case
    error = res.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert field in [d["field"] for d in error["details"]], error
    assert payment_count(h) == before


def test_requires_an_idempotency_key(h):
    res = h.request("POST", "/v1/payments", json_body=new_payment())
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_rejects_malformed_json(h):
    res = h.request("POST", "/v1/payments", content=b"{not json",
                    headers={"Content-Type": "application/json", "Idempotency-Key": "bad-json-key-1"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "INVALID_JSON"


class TestAuthentication:
    def test_missing_api_key_is_401(self, h):
        res = h.request("POST", "/v1/payments", json_body=new_payment(), api_key=None)
        assert res.status_code == 401 and res.json()["error"]["code"] == "UNAUTHORIZED"

    def test_invalid_api_key_is_401(self, h):
        assert h.request("GET", "/v1/payments", api_key="sk_thisKeyDoesNotExist12345").status_code == 401

    def test_revoked_api_key_is_401(self, h):
        key = create_customer_with_api_key(h.platform.pool, "C-REVOKE", "Revoked")
        assert h.request("GET", "/v1/payments", api_key=key["api_key"]).status_code == 200
        revoke_api_key(h.platform.pool, key["key_id"])
        assert h.request("GET", "/v1/payments", api_key=key["api_key"]).status_code == 401

    def test_cannot_pay_on_behalf_of_another_customer(self, h):
        res = h.submit(new_payment(customerId="SOMEONE-ELSE"))
        assert res.status_code == 403 and res.json()["error"]["code"] == "CUSTOMER_MISMATCH"

    def test_other_customers_payments_are_invisible(self, h):
        mine = h.submit(new_payment()).json()
        other = create_customer_with_api_key(h.platform.pool, "C-OTHER", "Other")["api_key"]
        # 404 (not 403) so payment ids can't be probed
        assert h.request("GET", f"/v1/payments/{mine['id']}", api_key=other).status_code == 404
        assert h.request("GET", f"/v1/payments/{mine['id']}/events", api_key=other).status_code == 404


def test_rate_limit_returns_429(harnesses):
    h = harnesses(rate_limit_per_minute=5)
    statuses = [h.request("GET", "/v1/payments").status_code for _ in range(7)]
    assert statuses[:5] == [200] * 5
    assert statuses[5:] == [429, 429]
