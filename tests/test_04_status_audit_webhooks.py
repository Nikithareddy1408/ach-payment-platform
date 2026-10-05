"""PDF requirements: "Status Retrieval", "Audit-ability", "Event Notifications"."""
import psycopg
import pytest

from app.auth import create_customer_with_api_key
from app.webhooks import is_private_address, sign_payload, verify_signature
from tests.harness import assert_valid_chain, new_payment, wait_for


class TestStatusRetrieval:
    def test_status_before_and_after_processing(self, h):
        pid = h.submit(new_payment()).json()["id"]
        assert h.request("GET", f"/v1/payments/{pid}").json()["status"] == "PENDING"
        h.start()
        h.wait_for_final(pid)
        assert h.request("GET", f"/v1/payments/{pid}").json()["status"] == "COMPLETED"

    def test_unknown_payment_is_404(self, h):
        res = h.request("GET", "/v1/payments/pay_" + "0" * 32)
        assert res.status_code == 404 and res.json()["error"]["code"] == "PAYMENT_NOT_FOUND"

    def test_list_filter_and_stable_cursor_pagination(self, h):
        created = [h.submit(new_payment()).json() for _ in range(7)]
        seen, cursor = [], None
        while True:
            page = h.request("GET", "/v1/payments?limit=3" + (f"&cursor={cursor}" if cursor else "")).json()
            seen += [p["id"] for p in page["data"]]
            cursor = page["nextCursor"]
            if not cursor:
                break
        assert seen == [p["id"] for p in reversed(created)]  # newest first, nothing missed or repeated
        by_ref = h.request("GET", f"/v1/payments?reference={created[2]['reference']}").json()["data"]
        assert [p["id"] for p in by_ref] == [created[2]["id"]]
        assert len(h.request("GET", "/v1/payments?status=PENDING").json()["data"]) == 7
        assert h.request("GET", "/v1/payments?cursor=garbage").status_code == 400


class TestAuditability:
    def test_every_change_is_recorded_with_reason_actor_time_and_request_id(self, h):
        h.start()
        res = h.request("POST", "/v1/payments", json_body=new_payment(destinationAccount="EXT-FLAKY-AUDIT"),
                        headers={"Idempotency-Key": "audit-key-0001", "X-Request-Id": "client-trace-123456"})
        h.wait_for_final(res.json()["id"])
        events = h.events(res.json()["id"])
        assert_valid_chain(events, "COMPLETED")
        assert len(events) == 7
        assert events[0]["actor"] == "api" and events[0]["requestId"] == "client-trace-123456"  # correlates with logs
        assert all(e["actor"].startswith("worker:") for e in events[1:])
        assert all(len(e["reason"]) > 10 and e["createdAt"] for e in events)
        assert [e["sequence"] for e in events] == sorted(e["sequence"] for e in events)

    def test_audit_log_is_append_only(self, h):
        pid = h.submit(new_payment()).json()["id"]
        for sql in ("UPDATE payment_events SET reason = 'edited' WHERE payment_id = %s", "DELETE FROM payment_events WHERE payment_id = %s"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
                h.query(sql, (pid,))


class TestWebhooks:
    @staticmethod
    def register(h, url, api_key="default"):
        return h.request("POST", "/v1/webhook-endpoints", json_body={"url": url, "description": "test"}, api_key=api_key)

    def test_register_returns_secret_exactly_once(self, h):
        created = self.register(h, "https://example.com/hooks")
        assert created.status_code == 201 and created.json()["secret"].startswith("whsec_")
        listed = h.request("GET", "/v1/webhook-endpoints").json()["data"]
        assert listed[0]["id"] == created.json()["id"] and "secret" not in listed[0]

    def test_one_signed_webhook_per_status_change_in_order(self, h, receivers):
        r = receivers()
        endpoint = self.register(h, r.url).json()
        h.start()
        pid = h.submit(new_payment()).json()["id"]
        h.wait_for_final(pid)
        h.wait_for_quiet()
        assert [w["json"]["type"] for w in r.received] == ["payment.pending", "payment.processing", "payment.completed"]
        for w in r.received:
            assert w["json"]["data"]["payment"]["id"] == pid
            assert w["headers"]["ACH-Event-Id"] == w["json"]["id"]
            assert verify_signature(endpoint["secret"], w["raw"], w["headers"]["ACH-Signature"])
            assert "VA10001" not in w["raw"]  # account numbers stay masked in webhooks too

    def test_signature_rejects_wrong_secret_tampering_and_replay(self):
        payload, now = '{"id":"evt_1"}', 1_800_000_000
        header = sign_payload("whsec_right", payload, now)
        assert verify_signature("whsec_right", payload, header, now=now)
        assert not verify_signature("whsec_wrong", payload, header, now=now)
        assert not verify_signature("whsec_right", payload + " ", header, now=now)
        assert not verify_signature("whsec_right", payload, header, now=now + 301)   # replayed 5+ minutes later
        assert not verify_signature("whsec_right", payload, None, now=now)

    def test_failing_receiver_is_retried_and_order_is_kept(self, h, receivers):
        r = receivers(lambda n: 500 if n <= 2 else 200)
        self.register(h, r.url)
        h.start()
        pid = h.submit(new_payment()).json()["id"]
        h.wait_for_final(pid)
        h.wait_for_quiet()
        assert [w["json"]["data"]["payment"]["status"] for w in r.received] == ["PENDING", "PROCESSING", "COMPLETED"]
        assert h.query("SELECT state, attempts FROM webhook_deliveries ORDER BY id LIMIT 1")[0] == {"state": "DELIVERED", "attempts": 3}

    def test_dead_receiver_never_blocks_payments_and_failed_deliveries_can_be_retried(self, h, receivers):
        up = []
        r = receivers(lambda n: 200 if up else 503)
        self.register(h, r.url)
        h.start()
        pid = h.submit(new_payment()).json()["id"]
        assert h.wait_for_final(pid)["status"] == "COMPLETED"
        h.wait_for_quiet()
        failed = h.query("SELECT id, attempts FROM webhook_deliveries WHERE state = 'FAILED' ORDER BY id")
        assert len(failed) == 3 and all(f["attempts"] == 4 for f in failed)
        up.append(True)  # the customer fixes their server and asks us to re-send
        for f in failed:
            assert h.request("POST", f"/v1/webhook-deliveries/{f['id']}/retry").status_code == 202
        wait_for(lambda: len(r.received) == 3, 5, "manual retries to arrive")
        assert h.request("POST", f"/v1/webhook-deliveries/{failed[0]['id']}/retry").status_code == 409

    def test_customers_only_get_their_own_events_and_disabled_endpoints_get_nothing(self, h, receivers):
        mine, theirs, disabled = receivers(), receivers(), receivers()
        other = create_customer_with_api_key(h.platform.pool, "C-OTHER", "Other")["api_key"]
        self.register(h, mine.url)
        self.register(h, theirs.url, api_key=other)
        endpoint = self.register(h, disabled.url).json()
        assert h.request("DELETE", f"/v1/webhook-endpoints/{endpoint['id']}").json()["enabled"] is False
        h.start()
        pid = h.submit(new_payment()).json()["id"]
        h.wait_for_final(pid)
        h.wait_for_quiet()
        assert len(mine.received) == 3 and theirs.received == [] and disabled.calls == 0

    def test_ssrf_protection_refuses_private_network_urls(self, harnesses):
        h = harnesses(webhook_block_private_ips=True)
        for url in ["http://127.0.0.1/hook", "http://localhost:8080/hook", "http://10.0.0.5/admin",
                    "http://169.254.169.254/latest/meta-data/",  # cloud credentials endpoint
                    "http://[::1]/hook", "http://user:pass@example.com/hook", "ftp://example.com/hook"]:
            res = self.register(h, url)
            assert res.status_code == 400, url
            assert res.json()["error"]["details"][0]["field"] == "url"
        assert all(map(is_private_address, ["10.1.2.3", "172.20.0.1", "192.168.1.1", "100.64.0.1", "::ffff:127.0.0.1", "fd00::1"]))
        assert not any(map(is_private_address, ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"]))


def test_operations_endpoints(h):
    h.start()
    h.wait_for_final(h.submit(new_payment()).json()["id"])
    assert h.request("GET", "/health/live", api_key=None).status_code == 200
    assert h.request("GET", "/health/ready", api_key=None).json() == {"status": "ok", "database": "ok"}
    metrics = h.request("GET", "/metrics", api_key=None).text
    assert "ach_payments_created_total 1.0" in metrics
    assert 'ach_payment_transitions_total{to="COMPLETED"} 1.0' in metrics
    assert 'ach_bank_request_duration_seconds_count{outcome="accepted"} 1.0' in metrics
    assert "ach_oldest_open_payment_age_seconds 0.0" in metrics
    assert h.request("GET", "/docs", api_key=None).status_code == 200
