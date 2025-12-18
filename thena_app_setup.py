import os
import httpx
from dotenv import load_dotenv

load_dotenv()

THENA_API_KEY = os.getenv("THENA_API_KEY", "")
PUBLIC_BASE = os.getenv("PUBLIC_BASE", "").rstrip("/")  # e.g. https://your-service.trycloudflare.com
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "")
TEAM_IDS = [t.strip() for t in os.getenv("THENA_TEAM_IDS", "").split(",") if t.strip()]

CREATE_URL = "https://apps-studio.thena.ai/apps/create-app"
INSTALL_URL = "https://apps-studio.thena.ai/apps/install"


def must(cond, msg: str):
    if not cond:
        raise SystemExit(msg)


# Basic env validation
must(THENA_API_KEY, "Missing THENA_API_KEY in .env")
must(
    PUBLIC_BASE.startswith("https://"),
    "PUBLIC_BASE must be your https URL (Cloudflare / Render / etc.)",
)
must(WEBHOOK_TOKEN, "Missing WEBHOOK_TOKEN in .env")
must(TEAM_IDS, "Missing THENA_TEAM_IDS in .env (comma-separated)")


def with_token(path: str) -> str:
    """
    Build a URL like:
      https://your-domain/thena/events?token=WEBHOOK_TOKEN
    """
    return f"{PUBLIC_BASE}{path}?token={WEBHOOK_TOKEN}"


manifest = {
    "app": {
        "name": "Thena → PagerDuty (AssignedTo)",
        "description": "Triggers PagerDuty based on Thena ticket assignee",
        "category": "webhooks",
        "icons": {
            "small": "https://cdn1.iconfinder.com/data/icons/carbon-design-system-vol-8/32/webhook-1024.png",
            "large": "https://cdn1.iconfinder.com/data/icons/carbon-design-system-vol-8/32/webhook-1024.png",
        },
        "supported_locales": ["en-US"],
        # IMPORTANT: must be globally unique per Thena org
        "slug": "thena-pagerduty-assignedto-routing-0035",
    },
    "developer": {
        "name": "Hossam Hafez",
        "website": "https://dashboard.luciq.ai",
        "support_email": "hossamhafez@luciq.ai",
        "privacy_policy_url": "https://dashboard.luciq.ai/privacy",
        "terms_url": "https://dashboard.luciq.ai/terms",
        "documentation_url": "https://docs.luciq.ai",
    },
    "integration": {
        "entry_points": {
            "main": f"{PUBLIC_BASE}/app",
            "configuration": f"{PUBLIC_BASE}/config",
        },
        "webhooks": {
            # These MUST match your FastAPI routes
            "events": with_token("/thena/events"),
            "installations": with_token("/thena/installations"),
        },
        "interactivity": {
            "request_url": with_token("/thena/events"),
            "message_menu_option_url": with_token("/thena/events"),
        },
    },
    "configuration": {"required_settings": [], "optional_settings": []},
    "scopes": {
        "required": {
            "platform": [
                {
                    "scope": "webhooks:read",
                    "reason": "Read webhook data",
                    "description": "Access to read webhook information",
                }
            ]
        },
        "optional": {"platform": []},
    },
    # Empty subscribe = receive all platform events, you filter by eventType in your code
    "events": {"subscribe": [], "publish": []},
    "activities": [],
    "metadata": {"is_privileged_app": False},
}


async def post_json(client: httpx.AsyncClient, url: str, headers: dict, payload: dict) -> dict:
    r = await client.post(url, headers=headers, json=payload)
    if r.status_code >= 400:
        print("\n--- ERROR RESPONSE ---")
        print("URL:", url)
        print("Status:", r.status_code)
        print("Body:", r.text[:4000])
        print("--- END ERROR RESPONSE ---\n")
    r.raise_for_status()
    return r.json()


async def main():
    headers = {"Content-Type": "application/json", "x-api-key": THENA_API_KEY}

    # Step 1: Create app
    create_payload = {"app_visibility": "private", "manifest": manifest}

    async with httpx.AsyncClient(timeout=40) as client:
        created = await post_json(client, CREATE_URL, headers, create_payload)

    app_id = (
        created.get("appId")
        or created.get("uid")
        or created.get("data", {}).get("appId")
        or created.get("data", {}).get("uid")
    )
    must(app_id, f"Could not find appId/uid in create response: {created}")

    print("✅ Created app identifier:", app_id)

    # Step 2: Install app on team(s)
    install_payload = {
        "teamIds": TEAM_IDS,
        "appId": app_id,
        "appConfiguration": {"required_settings": [], "optional_settings": []},
    }

    async with httpx.AsyncClient(timeout=40) as client:
        installed = await post_json(client, INSTALL_URL, headers, install_payload)

    print("✅ Installed. Response:", installed)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())