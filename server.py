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
PD_EVENTS_URL = os.getenv("PD_EVENTS_URL", "https://events.pagerduty.com/v2/enqueue")

# Two PagerDuty services
PD_ROUTING_KEY_A = os.getenv("PD_ROUTING_KEY_A", "")
PD_ROUTING_KEY_B = os.getenv("PD_ROUTING_KEY_B", "")

WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "")

# Persistent state DB (avoid duplicates across restarts/concurrency)
STATE_DB_PATH = os.getenv("STATE_DB_PATH", "./state.db")

# Default group for unassigned tickets (triage)
# Set this to "A" or "B" depending on who should receive unassigned new tickets.
DEFAULT_SERVICE_GROUP = os.getenv("DEFAULT_SERVICE_GROUP", "A").strip().upper()

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
# Persistent state store (SQLite) with atomic "claim"
# -------------------------------------------------------------------
_db_lock = threading.Lock()


def _db_connect() -> sqlite3.Connection:
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
                CREATE TABLE IF NOT EXISTS ticket_state (
                    ticket_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,                 -- processing | triggered
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
                """
            )
            conn.commit()
            conn.close()
        logger.info("State DB initialized at path=%s", STATE_DB_PATH)
    except Exception as e:
        logger.exception("Failed to initialize state DB at %s: %s", STATE_DB_PATH, e)


def is_ticket_handled(ticket_id: str) -> bool:
    """
    If state is 'processing' or 'triggered', we treat it as handled to prevent duplicates.
    """
    try:
        with _db_lock:
            conn = _db_connect()
            cur = conn.execute(
                "SELECT state FROM ticket_state WHERE ticket_id = ? LIMIT 1",
                (ticket_id,),
            )
            row = cur.fetchone()
            conn.close()
            if not row:
                return False
            return row[0] in ("processing", "triggered")
    except Exception as e:
        logger.exception("State DB read failed for ticket_id=%s: %s", ticket_id, e)
        # Fail-safe: allow triggering if DB is broken (may duplicate)
        return False


def claim_ticket(ticket_id: str) -> bool:
    """
    Atomic claim: insert state='processing'. If it already exists, claim fails.
    """
    try:
        with _db_lock:
            conn = _db_connect()
            cur = conn.execute(
                "INSERT OR IGNORE INTO ticket_state(ticket_id, state) VALUES (?, 'processing')",
                (ticket_id,),
            )
            conn.commit()
            claimed = cur.rowcount == 1
            conn.close()
            return claimed
    except Exception as e:
        logger.exception("State DB claim failed for ticket_id=%s: %s", ticket_id, e)
        # Fail-safe: don't block triggering if DB is broken
        return True


def mark_triggered(ticket_id: str) -> None:
    try:
        with _db_lock:
            conn = _db_connect()
            conn.execute(
                "UPDATE ticket_state SET state='triggered', updated_at=datetime('now') WHERE ticket_id=?",
                (ticket_id,),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        logger.exception("State DB mark triggered failed for ticket_id=%s: %s", ticket_id, e)


def release_claim(ticket_id: str) -> None:
    """
    If PD trigger fails, release claim so we can retry on next event.
    """
    try:
        with _db_lock:
            conn = _db_connect()
            conn.execute("DELETE FROM ticket_state WHERE ticket_id=?", (ticket_id,))
            conn.commit()
            conn.close()
    except Exception as e:
        logger.exception("State DB release claim failed for ticket_id=%s: %s", ticket_id, e)


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
    try:
        body = await req.json()
        logger.debug("Parsed JSON body: %s", body)
        return body
    except Exception:
        logger.info("Request body is empty or not JSON; treating as probe.")
        return {}


def map_severity(priority: str | None) -> str:
    p = (priority or "").strip().lower()
    if p in ("p0", "sev0", "urgent", "critical", "high"):
        return "critical"
    if p in ("p1", "sev1", "medium"):
        return "error"
    if p in ("p2", "sev2", "low"):
        return "warning"
    return "info"


def extract_assigned_to(ticket: Dict[str, Any]) -> Optional[str]:
    assigned_to = ticket.get("assignedTo")
    logger.debug("Raw assignedTo field: %r", assigned_to)

    if assigned_to is None:
        return None

    if isinstance(assigned_to, str):
        v = assigned_to.strip()
        return v or None

    if isinstance(assigned_to, dict):
        email = assigned_to.get("email") or assigned_to.get("userEmail")
        if isinstance(email, str) and email.strip():
            return email.strip()

        uid = assigned_to.get("id") or assigned_to.get("userId")
        if isinstance(uid, str) and uid.strip():
            return uid.strip()

        return None

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


def resolve_routing_key_for_group(group: str) -> str:
    rk = SERVICE_GROUP_TO_ROUTING_KEY.get(group)
    if not rk:
        raise HTTPException(status_code=500, detail=f"No routing key configured for group {group}")
    return rk


async def trigger_pd_for_ticket(
    routing_key: str,
    ticket: Dict[str, Any],
    assignee_identifier: str,
    event_type: str,
) -> dict:
    if not routing_key:
        logger.error("Missing PagerDuty routing key when trying to trigger PD")
        raise HTTPException(status_code=500, detail="Missing PagerDuty routing key")

    ticket_id = ticket.get("id") or ticket.get("ticketId") or "unknown"
    title = ticket.get("title") or f"Thena ticket {ticket_id}"
    priority = ticket.get("priorityName") or ticket.get("priority")
    severity = map_severity(priority)

    # PagerDuty dedup for the same ticket => same incident
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
    return {"ok": True}


@app.get("/thena/events")
@app.head("/thena/events")
def thena_events_probe(req: Request):
    require_token(req)
    logger.info("Thena /thena/events probe (GET/HEAD) received")
    return {"ok": True, "probe": True}


@app.post("/thena/events")
async def thena_events(req: Request):
    """
    New behavior:

    - Only handle ticket:created and ticket:assigned
    - Trigger exactly once per ticket (atomic claim + persisted state)
    - If ticket:created has NO assignee => trigger to DEFAULT_SERVICE_GROUP (triage)
    - If ticket:created has assignee => route by assignee mapping
    - If ticket:assigned happens later => triggers only if ticket hasn't already been handled
      (re-assignments will not trigger due to state)
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

    # If already triggered/processing -> ignore
    if is_ticket_handled(ticket_id):
        logger.info(
            "ticketId=%s already handled (triggered/processing) -> ignoring eventType=%s",
            ticket_id,
            event_type,
        )
        return {
            "ok": True,
            "ignored": True,
            "reason": "ticket_already_handled",
            "ticketId": ticket_id,
            "eventType": event_type,
        }

    # Extract assignee (may be None)
    assignee_identifier = extract_assigned_to(ticket)
    logger.info(
        "ticketId=%s eventType=%s extracted assignee=%r",
        ticket_id,
        event_type,
        assignee_identifier,
    )

    # Decide routing
    chosen_group: Optional[str] = None
    effective_assignee_label: str = ""

    if assignee_identifier:
        # Route by assignee mapping
        chosen_group = ASSIGNEE_TO_SERVICE_GROUP.get(assignee_identifier)
        if not chosen_group:
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
        effective_assignee_label = assignee_identifier
    else:
        # No assignee:
        # - On creation => send to default triage group
        # - On assigned event with missing assignee => ignore (should not happen, but safe)
        if event_type == "ticket:created":
            chosen_group = DEFAULT_SERVICE_GROUP
            effective_assignee_label = "UNASSIGNED"
            logger.info(
                "ticketId=%s created with NO assignee -> routing to DEFAULT_SERVICE_GROUP=%s",
                ticket_id,
                chosen_group,
            )
        else:
            logger.info(
                "ticketId=%s assigned event but no assignee present -> ignoring",
                ticket_id,
            )
            return {
                "ok": True,
                "ignored": True,
                "reason": "no_assignee_on_assigned",
                "ticketId": ticket_id,
                "eventType": event_type,
            }

    # Resolve routing key
    routing_key = resolve_routing_key_for_group(chosen_group)

    # Atomic claim BEFORE triggering PD
    claimed = claim_ticket(ticket_id)
    if not claimed:
        logger.info(
            "ticketId=%s could not be claimed (already processing/triggered) -> ignoring",
            ticket_id,
        )
        return {
            "ok": True,
            "ignored": True,
            "reason": "ticket_already_claimed",
            "ticketId": ticket_id,
            "eventType": event_type,
        }

    logger.info(
        "ticketId=%s eventType=%s routing to group=%s assigneeLabel=%s -> triggering PD",
        ticket_id,
        event_type,
        chosen_group,
        effective_assignee_label,
    )

    try:
        pd_response = await trigger_pd_for_ticket(
            routing_key=routing_key,
            ticket=ticket,
            assignee_identifier=effective_assignee_label,
            event_type=event_type,
        )
        mark_triggered(ticket_id)
        logger.info("ticketId=%s marked as triggered in state DB", ticket_id)

        return {
            "ok": True,
            "pagerduty": pd_response,
            "ticketId": ticket_id,
            "assignee": assignee_identifier,
            "assigneeLabel": effective_assignee_label,
            "serviceGroup": chosen_group,
            "eventType": event_type,
        }
    except Exception as e:
        # release claim so it can retry later
        release_claim(ticket_id)
        logger.exception("ticketId=%s trigger failed; released claim. Error=%s", ticket_id, e)
        raise


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
