import os
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

PD_EVENTS_URL = os.getenv(
    "PD_EVENTS_URL",
    "https://events.pagerduty.com/v2/enqueue"
)

# Two PagerDuty services
PD_ROUTING_KEY_A = os.getenv("PD_ROUTING_KEY_A", "")
PD_ROUTING_KEY_B = os.getenv("PD_ROUTING_KEY_B", "")

WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "")

# Map each assignee -> service group A or B
ASSIGNEE_TO_SERVICE_GROUP: Dict[str, str] = {
    "hossamhafez@luciq.ai": "A",
    "mahmoudelfiqi@luciq.ai": "A",
    "ibrahimsalem@luciq.ai": "A",
    "bedourelborai@luciq.ai": "B",
    "omarabdelsattar@luciq.ai": "B",
    "mirettewagdy@luciq.ai": "B",
}

SERVICE_GROUP_TO_ROUTING_KEY: Dict[str, str] = {
    "A": PD_ROUTING_KEY_A,
    "B": PD_ROUTING_KEY_B,
}
DEFAULT_SERVICE_GROUP = os.getenv("DEFAULT_SERVICE_GROUP", "A")

# Optional simple idempotency by ticket
TICKETS_TRIGGERED: set[str] = set()


def require_token(req: Request):
    token = req.query_params.get("token", "")
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


async def safe_json(req: Request) -> dict:
    """
    Thena (or its validator) may send GET/HEAD, or POST with empty/non-JSON body.
    This prevents 400/500 during validation.
    """
    try:
        return await req.json()
    except Exception:
        return {}


def map_severity(priority: str | None) -> str:
    # PagerDuty allowed severities: critical|error|warning|info
    p = (priority or "").strip().lower()
    if p in ("p0", "sev0", "urgent", "critical", "high"):
        return "critical"
    if p in ("p1", "sev1", "medium"):
        return "error"
    if p in ("p2", "sev2", "low"):
        return "warning"
    return "info"


def extract_assigned_to(ticket: Dict[str, Any]) -> Optional[str]:
    """
    Extract primary assignee from ticket["assignedTo"].

    Handles:
      - null
      - string
      - dict with email/id
      - list of strings or dicts
    """
    assigned_to = ticket.get("assignedTo")

    if assigned_to is None:
        return None

    # Simple string (email / id)
    if isinstance(assigned_to, str):
        v = assigned_to.strip()
        return v or None

    # Single object
    if isinstance(assigned_to, dict):
        email = assigned_to.get("email") or assigned_to.get("userEmail")
        if isinstance(email, str) and email.strip():
            return email.strip()

        uid = assigned_to.get("id") or assigned_to.get("userId")
        if isinstance(uid, str) and uid.strip():
            return uid.strip()

        return None

    # List (we take the first)
    if isinstance(assigned_to, list) and assigned_to:
        first = assigned_to[0]

        if isinstance(first, str):
            v = first.strip()
            return v or None

        if isinstance(first, dict):
            email = first.get("email") or first.get("userEmail")
            if isinstance(email, str) and email.strip():
                return email.strip()

            uid = first.get("id") or first.get("userId")
            if isinstance(uid, str) and uid.strip():
                return uid.strip()

    return None


async def trigger_pd_for_ticket(
    routing_key: str,
    ticket: Dict[str, Any],
    assignee_identifier: str,
    event_type: str,
) -> dict:
    """
    Build and send PagerDuty event for a given ticket + assignee.
    """
    if not routing_key:
        raise HTTPException(status_code=500, detail="Missing PagerDuty routing key")

    ticket_id = ticket.get("id") or ticket.get("ticketId") or "unknown"
    title = ticket.get("title") or f"Thena ticket {ticket_id}"
    priority = ticket.get("priorityName") or ticket.get("priority")
    severity = map_severity(priority)

    dedup_key = f"thena-ticket-{ticket_id}"

    pd_event = {
        "routing_key": routing_key,
        "event_action": "trigger",
        "dedup_key": dedup_key,
        "payload": {
            "summary": f"[{assignee_identifier}] {title}",
            "source": "thena",
            "severity": severity,
            "custom_details": {
                "eventType": event_type,
                "ticketId": ticket_id,
                "priority": priority,
                "assignee": assignee_identifier,
                "team": ticket.get("teamName"),
                "customer_email": ticket.get("customerContactEmail"),
            },
        },
        "client": "Thena → PagerDuty Bridge",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(PD_EVENTS_URL, json=pd_event)
        r.raise_for_status()
        return r.json()


@app.get("/health")
def health():
    return {"ok": True}


# ---- Thena events endpoint ----
# Keep GET/HEAD so Thena validation never fails
@app.get("/thena/events")
@app.head("/thena/events")
def thena_events_probe(req: Request):
    require_token(req)
    return {"ok": True, "probe": True}


@app.post("/thena/events")
async def thena_events(req: Request):
    """
    Main Thena webhook:
      - Accepts validator pings (empty / non-JSON) and returns 200
      - For ticket:created / ticket:updated:
          * If assignedTo maps to A/B -> use that service
          * If assignedTo is null or unmapped -> use DEFAULT_SERVICE_GROUP
    """
    require_token(req)
    body = await safe_json(req)

    # If Thena validator posts empty/non-JSON, acknowledge with 200
    if not body:
        return {"ok": True, "probe": True}

    msg = body.get("message") or {}
    event_type = msg.get("eventType")
    payload = msg.get("payload") or {}
    ticket = payload.get("ticket") or {}

    # Only act on ticket events; others are ignored but 200
    if event_type not in ("ticket:created", "ticket:updated"):
        return {"ok": True, "ignored": True, "eventType": event_type}

    ticket_id = ticket.get("id") or msg.get("eventId") or "unknown"

    # Simple idempotency: if we already triggered for this ticket, skip
    if ticket_id in TICKETS_TRIGGERED:
        return {"ok": True, "ignored": True, "reason": "ticket_already_triggered"}

    # Extract assignee (may be None)
    assignee_identifier = extract_assigned_to(ticket)

    # Decide service group:
    # - If assignee is known → A/B based on ASSIGNEE_TO_SERVICE_GROUP
    # - If assignee is null or not mapped → fallback to DEFAULT_SERVICE_GROUP
    if assignee_identifier:
        group = ASSIGNEE_TO_SERVICE_GROUP.get(assignee_identifier, DEFAULT_SERVICE_GROUP)
    else:
        group = DEFAULT_SERVICE_GROUP

    routing_key = SERVICE_GROUP_TO_ROUTING_KEY.get(group)
    if not routing_key:
        raise HTTPException(
            status_code=500,
            detail=f"No routing key configured for group {group}",
        )

    pd_response = await trigger_pd_for_ticket(
        routing_key=routing_key,
        ticket=ticket,
        assignee_identifier=assignee_identifier or "unassigned",
        event_type=event_type,
    )

    # Mark ticket as triggered so we don't open multiple incidents for same ticket
    TICKETS_TRIGGERED.add(ticket_id)

    return {
        "ok": True,
        "pagerduty": pd_response,
        "ticketId": ticket_id,
        "assignee": assignee_identifier,
        "serviceGroup": group,
    }
# ---- Thena installations endpoint ----
# Keep GET/HEAD/POST for validation
@app.get("/thena/installations")
@app.head("/thena/installations")
def thena_installations_probe(req: Request):
    require_token(req)
    return {"ok": True, "probe": True}


@app.post("/thena/installations")
async def thena_installations(req: Request):
    require_token(req)
    _ = await safe_json(req)  # swallow empty/non-JSON
    return {"ok": True}
