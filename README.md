You can copy-paste this directly into README.md in the repo and tweak names as you like.

# Thena → PagerDuty Bridge

A small FastAPI-based webhook bridge that listens to **Thena** ticket events and triggers **PagerDuty** incidents based on the ticket assignee (`assignedTo`).

- When a Thena `ticket:created` (or `ticket:updated`) event arrives,
- The bridge inspects `ticket.assignedTo`,
- Maps the assignee to a logical service group (`A`, `B`, etc.),
- Then triggers a PagerDuty incident on the corresponding service using the PagerDuty Events API v2.

Designed to be deployed as a lightweight web service (e.g. on Render) and wired to Thena via a private webhook app.

---

## Features

- Receives Thena webhook events via HTTPS
- Supports `ticket:created` and `ticket:updated` events
- Extracts `assignedTo` from various shapes (string, dict, list)
- Routes incidents to one of two (or more) PagerDuty services based on assignee
- Optional default service when `assignedTo` is `null`
- Simple idempotency to avoid duplicate PD incidents per ticket
- Health and installation probe endpoints for Thena webhook validation

---

## High-Level Architecture

```text
Thena ──(webhook: ticket:created/updated)──▶ FastAPI bridge ──▶ PagerDuty Events API v2

- Thena: private webhook app configured with events URL + token
- FastAPI bridge: validates token, parses payload, chooses PD routing key
- PagerDuty: creates an incident on the target service


Key components:

server.py – FastAPI app that handles Thena events and talks to PagerDuty

thena_app_setup.py – Helper script to create/install the Thena private webhook app pointing at the bridge

requirements.txt – Python dependencies

Requirements

Python 3.11+ (3.10/3.12 likely fine)

A Thena organization + API key

One or more PagerDuty services with Events API v2 integration keys

Public HTTPS endpoint (e.g. Render, Cloudflare Tunnel, etc.)

Environment Variables

The service and setup script are configured via environment variables.

Bridge (FastAPI server)

Used in server.py:

Variable	Required	Description
PD_EVENTS_URL	Yes	PagerDuty Events API base URL (e.g. https://events.pagerduty.com/v2/enqueue or EU URL).
PD_ROUTING_KEY_A	Yes	PagerDuty routing key (Events API v2 integration key) for Service A.
PD_ROUTING_KEY_B	Yes	PagerDuty routing key for Service B.
WEBHOOK_TOKEN	Yes	Shared secret token used as ?token=... on webhook URLs.
DEFAULT_SERVICE_GROUP	No	Default service group when assignedTo is null or unmapped (e.g. A).

You can extend server.py to support more services/groups if needed.

Thena app setup script

Used in thena_app_setup.py:

Variable	Required	Description
THENA_API_KEY	Yes	Thena API key used to create/install the private app.
THENA_TEAM_IDS	Yes	Comma-separated list of Thena team IDs to install the app on (e.g. TLL2UCCRSG).
PUBLIC_BASE	Yes	Public base URL of your deployed bridge (e.g. https://your-service.onrender.com).
WEBHOOK_TOKEN	Yes	Same token used by the FastAPI server, appended as query parameter for Thena webhooks.

Note: PUBLIC_BASE is only used by the setup script to build URLs for Thena.
On the deployed service (Render), you don’t need PUBLIC_BASE.

Local Setup

Clone the repo and install dependencies:

git clone git@github.com:YOUR_USER/thena-pagerduty-bridge.git
cd thena-pagerduty-bridge

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt


Create a .env file for local testing (do not commit this):

PD_EVENTS_URL=https://events.pagerduty.com/v2/enqueue
PD_ROUTING_KEY_A=<YOUR_PD_ROUTING_KEY_FOR_SERVICE_A>
PD_ROUTING_KEY_B=<YOUR_PD_ROUTING_KEY_FOR_SERVICE_B>
WEBHOOK_TOKEN=<YOUR_WEBHOOK_TOKEN>
DEFAULT_SERVICE_GROUP=A


Run the FastAPI app locally:

uvicorn server:app --host 0.0.0.0 --port 8000 --reload


Test health:

curl "http://localhost:8000/health"
# {"ok": true}

Local Webhook Test

Simulate a Thena ticket:created event hitting the local server:

curl -X POST \
  "http://localhost:8000/thena/events?token=<YOUR_WEBHOOK_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "eventId": "local-test-001",
      "eventType": "ticket:created",
      "payload": {
        "ticket": {
          "id": "LOCAL-TICKET-001",
          "title": "Local test ticket",
          "priorityName": "High",
          "assignedTo": [
            { "email": "someone@example.com" }
          ],
          "teamName": "Support",
          "customerContactEmail": "customer@example.com"
        }
      }
    }
  }'


The bridge will:

Validate token

Parse the assignedTo field

Map the assignee to a service group (A/B)

Trigger a PagerDuty event on PD_EVENTS_URL with the corresponding routing key

Thena App Setup

To let Thena send events to this bridge, you must create and install a private webhook app using Thena’s apps-studio APIs.

Configure a .env for the setup script:

THENA_API_KEY=<YOUR_THENA_API_KEY>
THENA_TEAM_IDS=TLL2UCCRSG             # or multiple team IDs, comma-separated
PUBLIC_BASE=https://your-service.onrender.com
WEBHOOK_TOKEN=<YOUR_WEBHOOK_TOKEN>    # same as in server env


Run the setup script:

python3 thena_app_setup.py


Example output:

✅ Created app identifier: SPC6MKCK108BZXFG0ZXEK3R456HVX
✅ Installed. Response: {... "appInstalledForTeams": ["TLL2UCCRSG"], ...}


This creates a private app in Thena with:

Events webhook URL: https://your-service.onrender.com/thena/events?token=...

Installations webhook URL: https://your-service.onrender.com/thena/installations?token=...

Thena will then start POSTing ticket events to your bridge.

Deployment (Render Example)

This project is designed to work very well on Render
 or any similar PaaS.

Typical Render configuration:

Environment: Python

Build Command:

pip install -r requirements.txt


Start Command:

uvicorn server:app --host 0.0.0.0 --port 8000


Health Check Path: /health

Environment Variables: set PD_EVENTS_URL, PD_ROUTING_KEY_A/B, WEBHOOK_TOKEN, DEFAULT_SERVICE_GROUP.

After deploy, Render will give you a URL like:

https://thena-pagerduty-bridge.onrender.com


Use that as PUBLIC_BASE when running thena_app_setup.py.

Routing Logic

Routing is driven by a mapping in server.py:

ASSIGNEE_TO_SERVICE_GROUP = {
    "hossamhafez@luciq.ai": "A",
    "mahmoudelfiqi@luciq.ai": "A",
    "ibrahimsalem@luciq.ai": "A",
    "bedourelborai@luciq.ai": "B",
    "omarabdelsattar@luciq.ai": "B",
    "mirettewagdy@luciq.ai": "B",
}

SERVICE_GROUP_TO_ROUTING_KEY = {
    "A": PD_ROUTING_KEY_A,
    "B": PD_ROUTING_KEY_B,
}


Behavior:

If assignedTo is present and mapped → use the corresponding group’s routing key.

If assignedTo is null or unmapped → use DEFAULT_SERVICE_GROUP (if set) or ignore, depending on your logic.

Each ticket.id is only triggered once per server lifetime via an in-memory TICKETS_TRIGGERED set.

You can customize:

The mapping table

Default behavior for null assignedTo

Priority/severity mapping for PagerDuty

Testing End-to-End

Once deployed and wired to Thena:

Ensure /health returns {"ok": true} on the public URL.

Use a curl test like above against /thena/events on the public URL.

Check PagerDuty: a new incident should appear on the expected service.

Create a real ticket in Thena and verify that:

The bridge logs the event (on your PaaS logs).

A PD incident is created with the expected summary and custom details.

License

Choose a license appropriate for your use (e.g. MIT, Apache-2.0) and add it here.


---

## 2) Internal Documentation (for Luciq)

You can paste this into Confluence / Notion as an internal page. It reuses the concepts but adds ownership, runbooks, and Luciq-specific details.

```markdown
# Thena → PagerDuty Bridge (Luciq Internal)

## 1. Purpose

This service connects **Thena** ticketing with **PagerDuty** for Luciq.

When a ticket is created or updated in Thena, the bridge:

- receives the Thena webhook event,
- inspects `ticket.assignedTo`,
- maps the assignee to a logical service group (`A` / `B`),
- triggers a PagerDuty incident on the corresponding service.

This provides automated on-call alerting based on Thena ticket ownership.

---

## 2. Ownership

- **Service owner**: Luciq Support / Tech Ops (primary: Hossam Hafez)
- **Code repo**: `https://github.com/hhafez300/thena-pagerduty-bridge`
- **Hosting**: Render (web service)
- **Upstream systems**:
  - Thena (team: `TLL2UCCRSG` – Luciq support)
  - PagerDuty (two services, one for Service Group A, one for Service Group B)

---

## 3. High-Level Design

### Components

1. **FastAPI bridge (`server.py`)**
   - Exposes:
     - `GET /health` – health check
     - `GET/HEAD/POST /thena/events` – main Thena webhook endpoint
     - `GET/HEAD/POST /thena/installations` – Thena app installation webhook (mostly for validation)
   - Validates the `?token=...` query parameter (shared secret).
   - Parses the Thena payload, extracts the ticket and its assignee, and triggers PagerDuty.

2. **Thena private webhook app**
   - Created via `thena_app_setup.py` using Thena’s `apps-studio` API.
   - Wired to the bridge endpoints:
     - Events: `<RENDER_URL>/thena/events?token=...`
     - Installations: `<RENDER_URL>/thena/installations?token=...`

3. **PagerDuty services**
   - Service A: used for assignees mapped to group `A`.
   - Service B: used for assignees mapped to group `B`.
   - Both configured with Events API v2 integration keys (`PD_ROUTING_KEY_A` and `PD_ROUTING_KEY_B`).

---

## 4. Routing Rules (Luciq)

Current mapping in `server.py`:

```python
ASSIGNEE_TO_SERVICE_GROUP = {
    "hossamhafez@luciq.ai": "A",
    "mahmoudelfiqi@luciq.ai": "A",
    "ibrahimsalem@luciq.ai": "A",

    "bedourelborai@luciq.ai": "B",
    "omarabdelsattar@luciq.ai": "B",
    "mirettewagdy@luciq.ai": "B",
}

SERVICE_GROUP_TO_ROUTING_KEY = {
    "A": PD_ROUTING_KEY_A,  # PagerDuty Service A
    "B": PD_ROUTING_KEY_B,  # PagerDuty Service B
}


Default behavior (configurable via env):

If assignedTo is null or unmapped:

Use DEFAULT_SERVICE_GROUP, currently set to A (if configured).

If not configured, the event may be ignored (no PD incident).

Ticket-level idempotency:

Each ticket.id will only trigger one PD incident per process lifetime; tracked in an in-memory set TICKETS_TRIGGERED.

5. Environment & Deployment
Render

Service name: thena-pagerduty-bridge (or equivalent)

Health check path: /health

Build command:

pip install -r requirements.txt


Start command:

uvicorn server:app --host 0.0.0.0 --port 8000

Environment variables on Render
# Thena / webhook
WEBHOOK_TOKEN=*** shared secret used in ?token=...
DEFAULT_SERVICE_GROUP=A

# PagerDuty
PD_EVENTS_URL=https://events.pagerduty.com/v2/enqueue  # or EU variant if used
PD_ROUTING_KEY_A=***  # Service A Events API v2 key
PD_ROUTING_KEY_B=***  # Service B Events API v2 key


Thena-specific variables (THENA_API_KEY, THENA_TEAM_IDS, PUBLIC_BASE) are used only locally by thena_app_setup.py to create/install the Thena app. They are not needed in Render.

6. Thena Integration Details

Thena team: TLL2UCCRSG (“Luciq support”).

Thena webhook app:

Created as a private app via apps-studio.

Event URL: <RENDER_URL>/thena/events?token=<WEBHOOK_TOKEN>

Installations URL: <RENDER_URL>/thena/installations?token=<WEBHOOK_TOKEN>

Setup Script (thena_app_setup.py)

Local .env example (for running the script):

THENA_API_KEY=pk_live_********
THENA_TEAM_IDS=TLL2UCCRSG
PUBLIC_BASE=https://thena-pagerduty-bridge.onrender.com
WEBHOOK_TOKEN=********


Run:

python3 thena_app_setup.py


This:

Calls https://apps-studio.thena.ai/apps/create-app with a manifest pointing to <PUBLIC_BASE>/thena/....

Installs the app on team TLL2UCCRSG via https://apps-studio.thena.ai/apps/install.

7. Operational Runbook
7.1. How to check if the service is up

Go to Render → thena-pagerduty-bridge service.

Confirm status is Live.

Call health endpoint:

curl "<RENDER_URL>/health"


Expected:

{"ok": true}

7.2. How to test end-to-end (synthetic)

Use curl to simulate Thena events hitting the bridge.

Test: null assignedTo (default group)

curl -X POST \
  "<RENDER_URL>/thena/events?token=<WEBHOOK_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "eventId": "prod-test-null-001",
      "eventType": "ticket:created",
      "payload": {
        "ticket": {
          "id": "PROD-TICKET-NULL-001",
          "title": "Prod test ticket with null assignedTo",
          "priorityName": "Medium",
          "assignedTo": null,
          "teamName": "Luciq support",
          "customerContactEmail": "customer@example.com"
        }
      }
    }
  }'


Check:

Response includes "ok": true and "serviceGroup": "A" (if DEFAULT_SERVICE_GROUP=A).

PagerDuty shows a new incident on Service A.

Test: Service A routing

curl -X POST \
  "<RENDER_URL>/thena/events?token=<WEBHOOK_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "eventId": "prod-test-A-001",
      "eventType": "ticket:created",
      "payload": {
        "ticket": {
          "id": "PROD-TICKET-A-001",
          "title": "Prod test ticket to Service A",
          "priorityName": "High",
          "assignedTo": [
            { "email": "hossamhafez@luciq.ai" }
          ],
          "teamName": "Luciq support",
          "customerContactEmail": "customer@example.com"
        }
      }
    }
  }'


Test: Service B routing

curl -X POST \
  "<RENDER_URL>/thena/events?token=<WEBHOOK_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "eventId": "prod-test-B-001",
      "eventType": "ticket:created",
      "payload": {
        "ticket": {
          "id": "PROD-TICKET-B-001",
          "title": "Prod test ticket to Service B",
          "priorityName": "Medium",
          "assignedTo": [
            { "email": "bedourelborai@luciq.ai" }
          ],
          "teamName": "Luciq support",
          "customerContactEmail": "customer@example.com"
        }
      }
    }
  }'


Check that each goes to the correct PagerDuty service.

7.3. How to rotate secrets

PagerDuty routing keys:

Create new Events API v2 integration keys in PagerDuty.

Update PD_ROUTING_KEY_A / PD_ROUTING_KEY_B in Render env vars.

Redeploy (or let Render auto-redeploy).

Run synthetic curl tests above.

WEBHOOK_TOKEN:

Generate a new token.

Update WEBHOOK_TOKEN in Render.

Re-run thena_app_setup.py or update the Thena app configuration so URLs include the new token.

Test /thena/events?token=<NEW_TOKEN> with curl.

THENA_API_KEY:

Only used locally for thena_app_setup.py. Update local .env when key rotates.

8. Common Issues & Troubleshooting

Symptoms: Thena tickets no longer create PD incidents.

Check Render logs

Look for:

Auth errors from PagerDuty (401/403) → likely routing key issue.

401 “Invalid token” responses on /thena/events → Thena still using old WEBHOOK_TOKEN.

JSON key errors (e.g. payload shape changed in Thena).

Check health endpoint

curl <RENDER_URL>/health → if not {"ok": true}, app is unhealthy.

Check Thena app configuration

Verify the events webhook URL and token.

Confirm the app is installed on team TLL2UCCRSG.

Check PD dashboard

If PD is receiving events but not creating incidents, verify:

Correct integration type (Events v2).

Routing keys match what is in Render.

9. Change Management

When to update the service:

New owners added or responsibility shifts:

Update ASSIGNEE_TO_SERVICE_GROUP mapping in server.py.

Commit, push, let Render redeploy.

Optionally add a short change log in internal wiki.

New Thena teams or PD services:

Either:

Add more mappings/groups in the existing bridge, or

Deploy a second instance with different env and manifest.

Priority / severity model changes:

Update the severity-mapping function in server.py to reflect new internal standards.


If you want, I can also add a short “Changelog” section template to the internal doc so you can track iterations (v1, v2 with filters, etc.).
::contentReference[oaicite:0]{index=0}
