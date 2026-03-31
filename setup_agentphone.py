"""
setup_agentphone.py
====================
Run this ONCE to create your agent and phone number on AgentPhone.
It will print out your phone number and agent ID.

Usage:
    python setup_agentphone.py

Requires:
    AGENTPHONE_API_KEY environment variable
    WEBHOOK_URL environment variable (your server's public URL + /webhook)
"""

import os
import sys
import json
import urllib.request
import urllib.error

API_BASE = "https://api.agentphone.to"
API_KEY = os.environ.get("AGENTPHONE_API_KEY")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # e.g. https://your-app.railway.app/webhook


def api_call(method, path, body=None):
    """Make an API call to AgentPhone."""
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        print(f"API Error {e.code}: {error_body}")
        sys.exit(1)


def main():
    if not API_KEY:
        print("ERROR: Set AGENTPHONE_API_KEY environment variable first!")
        print("  Get your key at: https://agentphone.to (sign up → dashboard → API keys)")
        sys.exit(1)

    if not WEBHOOK_URL:
        print("ERROR: Set WEBHOOK_URL environment variable!")
        print("  This should be your server's public URL + /webhook")
        print("  Example: https://your-app.railway.app/webhook")
        sys.exit(1)

    print("=" * 60)
    print("  AgentPhone Setup")
    print("=" * 60)

    # Step 1: Set up project webhook
    print("\n1. Setting project webhook...")
    webhook_result = api_call("POST", "/v1/webhooks", {"url": WEBHOOK_URL})
    print(f"   Webhook URL set to: {WEBHOOK_URL}")

    # Step 2: Create the agent (webhook voice mode — we handle responses)
    print("\n2. Creating your personal assistant agent...")
    agent = api_call("POST", "/v1/agents", {
        "name": "Personal Assistant",
        "description": "My personal AI assistant that checks email and calendar",
        "voiceMode": "webhook",
        "beginMessage": "Hey! What can I help you with?",
        # "transferNumber": "+1XXXXXXXXXX",  # Optional: number to transfer calls to
    })
    agent_id = agent.get("id") or agent.get("agentId")
    print(f"   Agent created! ID: {agent_id}")

    # Step 3: Provision a phone number
    print("\n3. Provisioning a US phone number...")
    number = api_call("POST", "/v1/numbers", {
        "country": "US",
        "agentId": agent_id,
    })
    phone_number = number.get("number") or number.get("phoneNumber")
    number_id = number.get("id") or number.get("numberId")
    print(f"   Phone number: {phone_number}")

    # Done!
    print("\n" + "=" * 60)
    print("  SETUP COMPLETE!")
    print("=" * 60)
    print(f"\n  Your phone number:  {phone_number}")
    print(f"  Your agent ID:      {agent_id}")
    print(f"  Webhook URL:        {WEBHOOK_URL}")
    print(f"\n  Call {phone_number} to talk to your AI assistant!")
    print(f"\n  Save these values — you'll see them in your")
    print(f"  AgentPhone dashboard too: https://agentphone.to")
    print("=" * 60)


if __name__ == "__main__":
    main()
