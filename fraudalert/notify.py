import logging

import httpx

from fraudalert.config import Settings
from fraudalert.ingest.parsers import SINPE
from fraudalert.models import OtpRequest, Transaction
from fraudalert.priorities import NAME, OTP_RANK, rank

log = logging.getLogger(__name__)


def notify(settings: Settings, txn: Transaction, reasons: list[str]) -> bool:
    """POST to the configured webhook. The `text` key works with Slack/Discord/Mattermost-style hooks."""
    if not settings.notify_webhook_url:
        return False
    priority = NAME[min((rank(a.severity) for a in txn.alerts), default=3)].upper()
    text = (
        f"[{priority}] Transaction alarm: {txn.amount} {txn.currency} at {txn.merchant or 'unknown merchant'}"
        f" on {txn.occurred_at:%Y-%m-%d %H:%M}"
        + (" (SINPE transfer)" if txn.source == SINPE else f" (card …{txn.card_last4})" if txn.card_last4 else "")
        + "\n" + "\n".join(f"• {r}" for r in reasons)
    )
    payload = {
        "text": text,
        "content": text,  # Discord
        "transaction": {
            "id": txn.id,
            "amount": str(txn.amount),
            "currency": txn.currency,
            "merchant": txn.merchant,
            "occurred_at": txn.occurred_at.isoformat(),
            "card_last4": txn.card_last4,
            "anomaly_score": txn.anomaly_score,
        },
        "priority": priority,
        "reasons": reasons,
    }
    try:
        httpx.post(settings.notify_webhook_url, json=payload, timeout=10).raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.warning("webhook notification failed: %s", exc)
        return False


def notify_otp(settings: Settings, otp: OtpRequest) -> bool:
    """An OTP request is always Critical: someone is trying to complete a purchase with your card."""
    if not settings.notify_webhook_url:
        return False
    what = " ".join(x for x in (
        f"{otp.amount} {otp.currency}" if otp.amount is not None else "",
        f"at {otp.merchant}" if otp.merchant else "",
        f"(card …{otp.card_last4})" if otp.card_last4 else "") if x)
    priority = NAME[OTP_RANK].upper()
    text = (f"[{priority}] OTP request{': ' + what if what else ''} on {otp.received_at:%Y-%m-%d %H:%M}\n"
            f"• Your bank sent a one-time code to confirm a purchase. If you didn't ask for it, someone is using "
            f"your card details: don't share the code, and call your bank.")
    payload = {
        "text": text,
        "content": text,  # Discord
        "otp_request": {
            "id": otp.id,
            "received_at": otp.received_at.isoformat(),
            "subject": otp.subject,
            "merchant": otp.merchant,
            "amount": None if otp.amount is None else str(otp.amount),
            "currency": otp.currency,
            "card_last4": otp.card_last4,
        },
        "priority": priority,
        "reasons": ["OTP request"],
    }
    try:
        httpx.post(settings.notify_webhook_url, json=payload, timeout=10).raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.warning("webhook notification failed: %s", exc)
        return False
