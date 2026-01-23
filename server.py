import os
import logging
import sqlite3
import threading
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv

# -------------------------------------------------------------------
# Logging setup
# -------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("thena-pagerduty-bridge")

load_dotenv()

app = FastAPI()

# -------------------------------------------------------------------
# Config
# -------------------------------------------------------------------
PD_EVENTS_URL = os.getenv(
    "PD_EVENTS_URL",
    "https://events.pagerduty.com/v2/enqueue"
)

# Two PagerDuty services
PD_ROUTING_KEY_A = os.getenv("PD_ROUTING_KEY_A", "")
PD_ROUTING_KEY_B = os.getenv("PD_ROUTING_KEY_B", "")

WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "")

# Persistent state DB (to avoid duplicates across restarts / scale-to-zero)
STATE_DB_PATH = os.getenv("STATE_DB_PATH", "./state.db")

# Map each assignee -> service group A or B
ASSIGNEE_TO_SERVICE_GROUP: Dict[str, str] = {
    # Team A
    "mahmoudelfiqi@luciq.ai": "A",
    "UMM2LEELBR": "A",      # Fiqi

    "ibrahimsalem@luciq.ai": "A",
    "UXX91JJ92J": "A",      # Ibrahim

    "hossamhafez@luciq.ai": "A",
    "UAA43NN1Z1": "A",      # Hossam

    # Team B
    "mirettewagdy@luciq.ai": "B",
    "U55BD44N8J": "B",      # Mirette

    "omarabdelsattar@luciq.ai": "B",
    "UEEB866ZDO": "B",      # Omar

    "bedourelborai@luciq.ai": "B",
    "UPPEJ11K3H": "B",      # Bedour
}

SERVICE_GROUP_TO_ROUTING_KEY: Dict[str, str] = {
    "A": PD_ROUTING_KEY_A,
    "B": PD_ROUTING_KEY_B,
}

# -------------------------------------------------------------------
# Persistent state store (SQLite)
# -------------------------------------------------------------------
_db_lock = threading.Lock()


def _db_connect() -> sqlite3.Connection:
    # check_same_thread=False allows usage across async requests; we also protect with a lock.
    conn = sqlite3.connect(STATE_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_state_db():
    try:
        with _db_lock:
            conn = _db_connect()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS triggered_tickets (
                    ticket_id TEXT PRIMARY KEY,
                    triggered_at TEXT DEFAULT (datetime('now'))
                )
                """
            )
            conn.commit()
            conn.close()
        logger.info("State DB initialized at path=%s", STATE_DB_PATH)
    except Exception as e:
        # If DB fails for any reason, we should log loudly because duplication protection might break.
        logger.exception("Failed to initialize state DB at %s: %s", STATE_DB_PATH, e)


def is_ticket_triggered(ticket_id: str) -> bool:
    try:
        with _db_lock:
            conn = _db_connect()
            cur = conn.execute(
                "SELECT 1 FROM triggered_tickets WHERE ticket_id = ? LIMIT 1",
                (ticket_id,),
            )
            row = cur.fetchone()
            conn.close()
            return row is not None
    except Exception as e:
        logger.exception("State DB read failed for ticket_id=%s: %s", ticket_id, e)
        # Fail-safe choice:
        # If DB read fails, assume NOT triggered so we don't lose alerting.
        # This might allow duplicates if DB is broken.
        return False


def mark_ticket_triggered(ticket_id: str) -> None:
    try:
        with _db_lock:
            conn = _db_connect()
            conn.execute(
                "INSERT OR IGNORE INTO triggered_tickets(ticket_id) VALUES (?)",
                (ticket_id,),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        logger.exception("State DB write failed for ticket_id=%s: %s", ticket_id, e)
        # Fail-safe: if write fails, we might get duplicates later, but we do not block the current request.


# Initialize DB at import time (works for Render)
init_state_db()

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def require_token(req: Request):
    token = req.query_params.get("token", "")
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        logger.warning("Unauthorized request: invalid token '%s'", token)
        raise HTTPException(status_code=401, detail="Invalid token")


async def safe_json(req: Request) -> dict:
    """
    Thena (or its validator) may send GET/HEAD, or POST with empty/non-JSON body.
    This prevents 400/500 during validation.
    """
    try:
        body = await req.json()
        logger.debug("Parsed JSON body: %s", body)
        return body
    except Exception:
        logger.info("Request body is empty or not JSON; treating as probe.")
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
    logger.debug("Raw assignedTo field: %r", assigned_to)

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
        logger.error("Missing PagerDuty routing key when trying to trigger PD")
        raise HTTPException(status_code=500, detail="Missing PagerDuty routing key")

    ticket_id = ticket.get("id") or ticket.get("ticketId") or "unknown"
    title = ticket.get("title") or f"Thena ticket {ticket_id}"
    priority = ticket.get("priorityName") or ticket.get("priority")
    severity = map_severity(priority)

    # Important: dedup_key keeps PD deduping the same ticket into one incident.
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

    logger.info(
        "Triggering PagerDuty for ticket_id=%s assignee=%s eventType=%s severity=%s",
        ticket_id,
        assignee_identifier,
        event_type,
        severity,
    )
    logger.debug("PagerDuty payload: %s", pd_event)

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(PD_EVENTS_URL, json=pd_event)

    if resp.status_code != 202:
        logger.error(
            "PagerDuty returned non-202 status %s for ticket_id=%s: %s",
            resp.status_code,
            ticket_id,
            resp.text,
        )
        raise HTTPException(status_code=500, detail="Failed to send event to PagerDuty")

    logger.info(
        "PagerDuty event accepted (202) for ticket_id=%s assignee=%s",
        ticket_id,
        assignee_identifier,
    )
    logger.debug("PagerDuty response body: %s", resp.text)

    return resp.json()


# -------------------------------------------------------------------
# FastAPI endpoints
# -------------------------------------------------------------------
@app.get("/health")
def health():
    logger.debug("Health check called")
    return {"ok": True}


# ---- Thena events endpoint ----
# Keep GET/HEAD so Thena validation never fails
@app.get("/thena/events")
@app.head("/thena/events")
def thena_events_probe(req: Request):
    require_token(req)
    logger.info("Thena /thena/events probe (GET/HEAD) received")
    return {"ok": True, "probe": True}


@app.post("/thena/events")
async def thena_events(req: Request):
    """
    Policy A behavior (clean, low-noise):

      - Accepts validator pings (empty / non-JSON) and returns 200

      - Trigger PagerDuty ONLY for new-ticket paging semantics:
          1) ticket:created
              - If NO assignee -> do nothing (wait)
              - If assignee is mapped -> trigger PD (once)

          2) ticket:assigned (can happen later, even hours later)
              - If already triggered -> do nothing
              - If assignee is mapped -> trigger PD (once)

      - NEVER trigger on ticket:updated or any other event types.
    """
    require_token(req)
    body = await safe_json(req)

    if not body:
        logger.info("Received empty/invalid body on /thena/events, treating as probe")
        return {"ok": True, "probe": True}

    msg = body.get("message") or {}
    event_type = msg.get("eventType")
    payload = msg.get("payload") or {}
    ticket = payload.get("ticket") or {}

    ticket_id = ticket.get("id") or ticket.get("ticketId") or msg.get("eventId") or "unknown"
    team_id = msg.get("teamId") or ticket.get("teamId")
    team_name = ticket.get("teamName")

    logger.info(
        "Incoming Thena event: eventType=%s ticketId=%s teamId=%s teamName=%s",
        event_type,
        ticket_id,
        team_id,
        team_name,
    )
    logger.debug("Full Thena payload: %s", body)

    # Only act on ticket:created / ticket:assigned
    if event_type not in ("ticket:created", "ticket:assigned"):
        logger.info(
            "Ignoring eventType=%s for ticketId=%s (not ticket:created/assigned)",
            event_type,
            ticket_id,
        )
        return {"ok": True, "ignored": True, "eventType": event_type}

    # Persistent idempotency: if we already fired PD for this ticket, never do it again
    if is_ticket_triggered(ticket_id):
        logger.info(
            "Ticket %s already triggered PagerDuty before (persisted), ignoring eventType=%s",
            ticket_id,
            event_type,
        )
        return {
            "ok": True,
            "ignored": True,
            "reason": "ticket_already_triggered",
            "ticketId": ticket_id,
            "eventType": event_type,
        }

    # Extract assignee (email or user id) from ticket.assignedTo
    assignee_identifier = extract_assigned_to(ticket)
    logger.info(
        "ticketId=%s eventType=%s extracted assignee=%r",
        ticket_id,
        event_type,
        assignee_identifier,
    )

    # If created but unassigned -> do nothing and wait for ticket:assigned later
    if event_type == "ticket:created" and not assignee_identifier:
        logger.info(
            "ticketId=%s created with NO assignee -> waiting for ticket:assigned",
            ticket_id,
        )
        return {
            "ok": True,
            "ignored": True,
            "reason": "no_assignee_on_create",
            "ticketId": ticket_id,
        }

    # If assigned event but still missing assignee -> ignore
    if event_type == "ticket:assigned" and not assignee_identifier:
        logger.info(
            "ticketId=%s assigned event but assignee is missing -> ignoring",
            ticket_id,
        )
        return {
            "ok": True,
            "ignored": True,
            "reason": "no_assignee_on_assigned",
            "ticketId": ticket_id,
            "eventType": event_type,
        }

    # Defensive check
    if not assignee_identifier:
        return {
            "ok": True,
            "ignored": True,
            "reason": "assignee_missing_defensive",
            "ticketId": ticket_id,
            "eventType": event_type,
        }

    # Map assignee -> group (A/B)
    group = ASSIGNEE_TO_SERVICE_GROUP.get(assignee_identifier)
    if not group:
        logger.info(
            "ticketId=%s assignee=%s has no PD mapping -> ignoring",
            ticket_id,
            assignee_identifier,
        )
        return {
            "ok": True,
            "ignored": True,
            "reason": "no_mapping_for_assignee",
            "assignee": assignee_identifier,
            "ticketId": ticket_id,
            "eventType": event_type,
        }

    routing_key = SERVICE_GROUP_TO_ROUTING_KEY.get(group)
    if not routing_key:
        logger.error(
            "ticketId=%s assignee=%s group=%s but NO routing key configured",
            ticket_id,
            assignee_identifier,
            group,
        )
        raise HTTPException(
            status_code=500,
            detail=f"No routing key configured for group {group}",
        )

    logger.info(
        "ticketId=%s eventType=%s assignee=%s mapped to serviceGroup=%s -> triggering PD",
        ticket_id,
        event_type,
        assignee_identifier,
        group,
    )

    pd_response = await trigger_pd_for_ticket(
        routing_key=routing_key,
        ticket=ticket,
        assignee_identifier=assignee_identifier,
        event_type=event_type,
    )

    # Mark ticket as triggered (persisted), so we never open multiple incidents even after restart
    mark_ticket_triggered(ticket_id)
    logger.info(
        "ticketId=%s marked as triggered in state DB",
        ticket_id,
    )

    return {
        "ok": True,
        "pagerduty": pd_response,
        "ticketId": ticket_id,
        "assignee": assignee_identifier,
        "serviceGroup": group,
        "eventType": event_type,
    }


# ---- Thena installations endpoint ----
# Keep GET/HEAD/POST for validation
@app.get("/thena/installations")
@app.head("/thena/installations")
def thena_installations_probe(req: Request):
    require_token(req)
    logger.info("Thena /thena/installations probe (GET/HEAD) received")
    return {"ok": True, "probe": True}


@app.post("/thena/installations")
async def thena_installations(req: Request):
    require_token(req)
    body = await safe_json(req)
    logger.info("Received Thena installations webhook: %s", body)
    return {"ok": True}
