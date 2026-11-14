"""EventBridge OrderConfirmed -> confirmation email via SES.

Decoupled from checkout: if SES is slow or the sandbox rejects a recipient,
seats are still sold and the checkout queue keeps draining. EventBridge
retries this target and failures are alarmed on.
"""
import logging
import os

import boto3
from botocore.exceptions import ClientError

from core import metrics, settings

log = logging.getLogger()
log.setLevel(logging.INFO)

ddb = boto3.client("dynamodb")
ses = boto3.client("ses")
SENDER = os.environ["SENDER_EMAIL"]


def seat_label(seat):
    section, sk = seat.split("|", 1)
    row, num = sk[1:3], sk[-2:]
    return f"Section {section} · Row {int(row)} · Seat {int(num)}"


def handler(event, context):
    d = event["detail"]
    t = settings.tables()
    order = ddb.get_item(TableName=t.orders, Key={"order_id": {"S": d["order_id"]}}).get("Item")
    if not order:
        raise RuntimeError(f"order {d['order_id']} not found")  # let EventBridge retry
    email = order["email"]["S"]
    seats = [seat_label(s["S"]) for s in order.get("seats", {}).get("L", [])]
    if not seats:
        seats = [f'{l["M"]["qty"]["N"]} × {l["M"]["section"]["S"]} (general admission)'
                 for l in order["lines"]["L"]]
    amount = f'{d["currency"]} {d["total"]:,}'
    lines_txt = "\n".join(f"  - {s}" for s in seats)
    text = (f"You're going to {d['artist']}!\n\n{d['title']}\n{d['venue']}, {d['city']}\n{d['starts_at']}\n\n"
            f"{lines_txt}\n\nTotal: {amount}\nTicket code: {d['ticket_code']}\nOrder: {d['order_id']}\n\n"
            "FrontRow is a portfolio demo. This is not a real ticket and no payment was taken.")
    rows = "".join(f"<li>{s}</li>" for s in seats)
    html = (f"<div style='font-family:system-ui,sans-serif;max-width:520px'>"
            f"<h2 style='margin:0'>You're going to {d['artist']}!</h2>"
            f"<p style='color:#555'>{d['title']}<br>{d['venue']}, {d['city']}<br>{d['starts_at']}</p>"
            f"<ul>{rows}</ul><p>Total: <b>{amount}</b><br>Ticket code: "
            f"<b style='font-family:monospace;font-size:18px'>{d['ticket_code']}</b></p>"
            f"<p style='color:#888;font-size:12px'>FrontRow is a portfolio demo. Not a real ticket; no payment taken.</p></div>")
    try:
        ses.send_email(Source=SENDER, Destination={"ToAddresses": [email]}, Message={
            "Subject": {"Data": f"Your tickets: {d['artist']} - {d['city']}"},
            "Body": {"Text": {"Data": text}, "Html": {"Data": html}}})
        metrics.emit("EmailsSent", service="notifier")
    except ClientError as err:
        code = err.response["Error"]["Code"]
        metrics.emit("EmailFailures", service="notifier", code=code)
        if code in ("MessageRejected", "MailFromDomainNotVerifiedException"):
            log.warning("SES rejected mail for %s: %s", d["order_id"], code)  # sandbox: unverified recipient
            return
        raise
