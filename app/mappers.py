"""Database rows -> public API shapes."""
from .domain import format_cents, mask_account


def to_api_payment(p: dict) -> dict:
    """Database row -> public API shape. Account numbers are masked (last 4 only), as Stripe does."""
    return {
        "id": p["id"],
        "customerId": p["customer_id"],
        "sourceAccount": mask_account(p["source_account"]),
        "destinationAccount": mask_account(p["destination_account"]),
        "amount": format_cents(p["amount_cents"]),
        "amountCents": p["amount_cents"],
        "currency": p["currency"],
        "reference": p["reference"],
        "status": p["status"],
        "attempts": p["attempts"],
        "bankTransferId": p["bank_transfer_id"],
        "lastError": {"code": p["last_error_code"], "message": p["last_error_message"] or ""} if p["last_error_code"] else None,
        "createdAt": p["created_at"].isoformat(),
        "updatedAt": p["updated_at"].isoformat(),
        "completedAt": p["completed_at"].isoformat() if p["completed_at"] else None,
    }


def to_api_event(e: dict) -> dict:
    return {
        "id": f"evt_{e['id']}",
        "sequence": e["id"],
        "from": e["from_status"],
        "to": e["to_status"],
        "reason": e["reason"],
        "actor": e["actor"],
        "requestId": e["request_id"],
        "metadata": e["metadata"],
        "createdAt": e["created_at"].isoformat(),
    }
