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

# Single PagerDuty service routing key (ONLY ONE SERVICE NOW)
PD_ROUTING_KEY = os.getenv("PD_ROUTING_KEY", "").strip()

WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()

# Persistent state DB (avoid duplicates across restarts/concurrency)
STATE_DB_PATH = os.getenv("STATE_DB_PATH", "./state.db")

# Team members (4 only). Used for optional allowlist behavior on assigned tickets.
# Keep both email + Thena user ID because Thena may send either.
TEAM_MEMBERS: set[str] = {
    # Mahmoud
    "mahmoudelfiqi@luciq.ai",
    "UMM2LEELBR",
    # Ibrahim
    "ibrahimsalem@luciq.ai",
    "UXX91JJ92J",
    # Hossam
    "hossamhafez@luciq.ai",
    "UAA43NN1Z1",
    # Omar
    "omarabdelsattar@luciq.ai",
    "UEEB866ZDO",
}

# If true: only trigger on ticket:assigned when assignee is one of TEAM_MEMBERS.
# If false: any assignee triggers (still routed to the single PD service).
REQUIRE_ASSIGNEE_IN_TEAM = os.getenv("REQUIRE_ASSIGNEE_IN_TEAM", "true").lower() == "true"

# -------------------------------------------------------------------
# Persistent state store (SQLite) with atomic claim
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
    If state is 'processing' or 'triggered', treat it as handled to prevent duplicates.
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
    # PagerDuty severities: critical|error|warning|info
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


def require_pd_routing_key():
    if not PD_ROUTING_KEY:
        logger.error("Missing PD_ROUTING_KEY env var")
        raise HTTPException(status_code=500, detail="Missing PD routing key configuration")


async def trigger_pd_for_ticket(
    ticket: Dict[str, Any],
    assignee_label: str,
    event_type: str,
) -> dict:
    """
    Build and send PagerDuty event for a given ticket.
    """
    require_pd_routing_key()

    ticket_id = ticket.get("id") or ticket.get("ticketId") or "unknown"
    title = ticket.get("title") or f"Thena ticket {ticket_id}"
    priority = ticket.get("priorityName") or ticket.get("priority")
    severity = map_severity(priority)

    # PD dedup for the same ticket => same incident
    dedup_key = f"thena-ticket-{ticket_id}"

    pd_event = {
        "routing_key": PD_ROUTING_KEY,
        "event_action": "trigger",
        "dedup_key": dedup_key,
        "payload": {
            "summary": f"[{assignee_label}] {title}",
            "source": "thena",
            "severity": severity,
            "custom_details": {
                "eventType": event_type,
                "ticketId": ticket_id,
                "priority": priority,
                "assignee": assignee_label,
                "team": ticket.get("teamName"),
                "customer_email": ticket.get("customerContactEmail"),
            },
        },
        "client": "Thena → PagerDuty Bridge",
    }

    logger.info(
        "Triggering PagerDuty for ticket_id=%s eventType=%s severity=%s assigneeLabel=%s",
        ticket_id,
        event_type,
        severity,
        assignee_label,
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

    logger.info("PagerDuty event accepted (202) for ticket_id=%s", ticket_id)
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
    Single-service behavior:

    - Only handle ticket:created and ticket:assigned
    - Trigger exactly once per ticket (atomic claim + persisted state)
    - ticket:created:
        * assigned => trigger once
        * unassigned => trigger once with assigneeLabel=UNASSIGNED
    - ticket:assigned:
        * triggers only if ticket not handled already
        * optional allowlist check controlled by REQUIRE_ASSIGNEE_IN_TEAM
    - Ignore ticket:updated and all other event types
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

    if event_type not in ("ticket:created", "ticket:assigned"):
        logger.info(
            "Ignoring eventType=%s for ticketId=%s (not ticket:created/assigned)",
            event_type,
            ticket_id,
        )
        return {"ok": True, "ignored": True, "eventType": event_type}

    # If already handled/processing -> ignore
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

    # On assigned events, optionally ensure assignee belongs to our 4-member team.
    if event_type == "ticket:assigned" and REQUIRE_ASSIGNEE_IN_TEAM:
        if not assignee_identifier:
            return {
                "ok": True,
                "ignored": True,
                "reason": "no_assignee_on_assigned",
                "ticketId": ticket_id,
                "eventType": event_type,
            }
        if assignee_identifier not in TEAM_MEMBERS:
            logger.info(
                "ticketId=%s assigned to %s which is not in TEAM_MEMBERS -> ignoring",
                ticket_id,
                assignee_identifier,
            )
            return {
                "ok": True,
                "ignored": True,
                "reason": "assignee_not_in_team",
                "assignee": assignee_identifier,
                "ticketId": ticket_id,
                "eventType": event_type,
            }

    # Determine label for PD summary
    if assignee_identifier:
        assignee_label = assignee_identifier
    else:
        # Unassigned ticket on creation => triage to same single service
        if event_type == "ticket:created":
            assignee_label = "UNASSIGNED"
        else:
            # assigned event without assignee present
            return {
                "ok": True,
                "ignored": True,
                "reason": "no_assignee_on_assigned",
                "ticketId": ticket_id,
                "eventType": event_type,
            }

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
        "ticketId=%s eventType=%s assigneeLabel=%s -> triggering PD (single service)",
        ticket_id,
        event_type,
        assignee_label,
    )

    try:
        pd_response = await trigger_pd_for_ticket(
            ticket=ticket,
            assignee_label=assignee_label,
            event_type=event_type,
        )
        mark_triggered(ticket_id)
        logger.info("ticketId=%s marked as triggered in state DB", ticket_id)

        return {
            "ok": True,
            "pagerduty": pd_response,
            "ticketId": ticket_id,
            "assignee": assignee_identifier,
            "assigneeLabel": assignee_label,
            "eventType": event_type,
        }
    except Exception as e:
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
