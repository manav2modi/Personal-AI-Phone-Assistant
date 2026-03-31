"""
Personal AI Phone Assistant
============================
Connects AgentPhone.to → Claude → Gmail & Google Calendar

When you call your AgentPhone number:
1. AgentPhone transcribes your voice → sends "agent.message" to this webhook
2. This server sends that text to Claude with Gmail/Calendar tools
3. Claude checks your email/calendar and crafts a spoken response
4. This server returns {"text": "..."} → AgentPhone speaks it back to you

Supports streaming responses (ndjson) so the caller hears "Let me check..."
immediately while tools run, instead of silence.

Run setup instructions in README.md first!
"""

import os
import json
import hmac
import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, request, jsonify, Response
import anthropic

# Google API imports
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
AGENTPHONE_WEBHOOK_SECRET = os.environ.get("AGENTPHONE_WEBHOOK_SECRET", "")

# Per-call timing tracker: callId → {last_response_at, turn_count}
_call_timings = {}

# Google OAuth scopes — read-only access to Gmail and Calendar
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]

# Transfer contacts — loaded from env var or hardcoded below.
# Env var format: TRANSFER_CONTACTS="john:+11234567890,sarah:+10987654321"
# Or edit the dict directly:
TRANSFER_CONTACTS = {}
_transfer_env = os.environ.get("TRANSFER_CONTACTS", "")
if _transfer_env:
    for entry in _transfer_env.split(","):
        entry = entry.strip()
        if ":" in entry:
            name, number = entry.split(":", 1)
            TRANSFER_CONTACTS[name.strip().lower()] = number.strip()

# STT often mishears names — map common misheard variants to the canonical name
# Env var format: TRANSFER_ALIASES="jon:john,sharah:sarah"
# Or edit the dict directly:
TRANSFER_ALIASES = {}
_aliases_env = os.environ.get("TRANSFER_ALIASES", "")
if _aliases_env:
    for entry in _aliases_env.split(","):
        entry = entry.strip()
        if ":" in entry:
            alias, canonical = entry.split(":", 1)
            TRANSFER_ALIASES[alias.strip().lower()] = canonical.strip().lower()

def _system_prompt():
    now = datetime.now()
    today = f"{now.strftime('%A, %B')} {now.day}, {now.year}"
    prompt = f"""You are a helpful personal assistant that can check the user's email and calendar.
Today's date is {today}.
You are speaking to the user over a phone call, so keep responses VERY short — 2-3 sentences max.
Don't use markdown, bullet points, or formatting — just speak naturally.
When listing events or emails, only mention the top 2-3 most important ones briefly.
Only use tools when the user explicitly asks about emails or calendar. For casual chat, just respond directly.
Be warm but brief — every extra word adds delay on a voice call.
When the user says goodbye or asks to hang up, say a brief goodbye like "Take care, bye!" — the system will end the call."""
    if TRANSFER_CONTACTS:
        contact_names = ", ".join(TRANSFER_CONTACTS.keys())
        prompt += f"\nYou can transfer the call to these contacts: {contact_names}. When the user asks to be transferred or connected to someone, say \"Transferring you to [name] now\" — the system will handle the transfer."
    return prompt

# ---------------------------------------------------------------------------
# Google Auth
# ---------------------------------------------------------------------------

def get_google_credentials():
    """
    Load Google OAuth creds from token.json or GOOGLE_TOKEN_JSON env var.
    On Railway/hosted environments, set GOOGLE_TOKEN_JSON to the contents
    of your locally-generated token.json file.
    """
    creds = None

    # Try env var first (for Railway / hosted deploys)
    token_json_str = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_json_str:
        creds = Credentials.from_authorized_user_info(
            json.loads(token_json_str), GOOGLE_SCOPES
        )
    elif os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", GOOGLE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
            # Persist refreshed token back to file if running locally
            if not token_json_str:
                with open("token.json", "w") as f:
                    f.write(creds.to_json())
        else:
            if not os.path.exists("credentials.json"):
                logger.error(
                    "Missing Google credentials! Either set GOOGLE_TOKEN_JSON env var "
                    "(for hosted deploys) or place credentials.json in the project folder "
                    "and run the OAuth flow locally. See README.md for instructions."
                )
                return None
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", GOOGLE_SCOPES
            )
            creds = flow.run_local_server(port=0)
            with open("token.json", "w") as f:
                f.write(creds.to_json())

    return creds


# ---------------------------------------------------------------------------
# Gmail & Calendar helpers (called by Claude as tools)
# ---------------------------------------------------------------------------

def get_recent_emails(max_results: int = 5) -> str:
    """Fetch recent inbox emails using batch to minimize API calls."""
    try:
        t0 = time.time()
        creds = get_google_credentials()
        if not creds:
            return "Gmail is not connected. Ask the user to check the server setup."

        service = build("gmail", "v1", credentials=creds)
        results = (
            service.users()
            .messages()
            .list(userId="me", maxResults=max_results, q="is:inbox")
            .execute()
        )
        messages = results.get("messages", [])
        if not messages:
            return "The inbox is empty — no recent emails."

        logger.info(f"Gmail list took {time.time()-t0:.2f}s")

        # Batch fetch all message details at once
        summaries = []
        t1 = time.time()

        def handle_message(request_id, response, exception):
            if exception:
                logger.error(f"Batch error: {exception}")
                return
            headers = {
                h["name"]: h["value"]
                for h in response.get("payload", {}).get("headers", [])
            }
            summaries.append(
                {
                    "from": headers.get("From", "Unknown"),
                    "subject": headers.get("Subject", "(no subject)"),
                    "date": headers.get("Date", ""),
                    "snippet": response.get("snippet", "")[:120],
                }
            )

        batch = service.new_batch_http_request(callback=handle_message)
        for msg in messages:
            batch.add(
                service.users().messages().get(
                    userId="me",
                    id=msg["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
            )
        batch.execute()

        logger.info(f"Gmail batch fetch took {time.time()-t1:.2f}s, total {time.time()-t0:.2f}s")
        return json.dumps(summaries, indent=2)
    except Exception as e:
        logger.error(f"Gmail error: {e}")
        return f"Error reading Gmail: {e}"


def search_emails(query: str, max_results: int = 5) -> str:
    """Search Gmail with a query string using batch fetch."""
    try:
        t0 = time.time()
        creds = get_google_credentials()
        if not creds:
            return "Gmail is not connected."

        service = build("gmail", "v1", credentials=creds)
        results = (
            service.users()
            .messages()
            .list(userId="me", maxResults=max_results, q=query)
            .execute()
        )
        messages = results.get("messages", [])
        if not messages:
            return f"No emails found matching: {query}"

        summaries = []

        def handle_message(request_id, response, exception):
            if exception:
                return
            headers = {
                h["name"]: h["value"]
                for h in response.get("payload", {}).get("headers", [])
            }
            summaries.append(
                {
                    "from": headers.get("From", "Unknown"),
                    "subject": headers.get("Subject", "(no subject)"),
                    "date": headers.get("Date", ""),
                    "snippet": response.get("snippet", "")[:120],
                }
            )

        batch = service.new_batch_http_request(callback=handle_message)
        for msg in messages:
            batch.add(
                service.users().messages().get(
                    userId="me",
                    id=msg["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
            )
        batch.execute()

        logger.info(f"Gmail search took {time.time()-t0:.2f}s")
        return json.dumps(summaries, indent=2)
    except Exception as e:
        logger.error(f"Gmail search error: {e}")
        return f"Error searching Gmail: {e}"


def get_todays_calendar() -> str:
    """Fetch today's calendar events."""
    try:
        creds = get_google_credentials()
        if not creds:
            return "Google Calendar is not connected."

        service = build("calendar", "v3", credentials=creds)

        now = datetime.now(timezone.utc)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)

        events_result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=start_of_day.isoformat(),
                timeMax=end_of_day.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        events = events_result.get("items", [])
        if not events:
            return "No events on your calendar today."

        summaries = []
        for event in events:
            start = event["start"].get("dateTime", event["start"].get("date"))
            summaries.append(
                {
                    "title": event.get("summary", "(no title)"),
                    "start": start,
                    "location": event.get("location", ""),
                    "description": (event.get("description") or "")[:100],
                }
            )

        return json.dumps(summaries, indent=2)
    except Exception as e:
        logger.error(f"Calendar error: {e}")
        return f"Error reading calendar: {e}"


def get_upcoming_events(days: int = 7) -> str:
    """Fetch calendar events for the next N days."""
    try:
        creds = get_google_credentials()
        if not creds:
            return "Google Calendar is not connected."

        service = build("calendar", "v3", credentials=creds)

        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days)

        events_result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=20,
            )
            .execute()
        )
        events = events_result.get("items", [])
        if not events:
            return f"No events in the next {days} days."

        summaries = []
        for event in events:
            start = event["start"].get("dateTime", event["start"].get("date"))
            summaries.append(
                {
                    "title": event.get("summary", "(no title)"),
                    "start": start,
                    "location": event.get("location", ""),
                }
            )

        return json.dumps(summaries, indent=2)
    except Exception as e:
        logger.error(f"Calendar error: {e}")
        return f"Error reading calendar: {e}"


# ---------------------------------------------------------------------------
# Claude tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "get_recent_emails",
        "description": "Get the most recent emails from the user's Gmail inbox. Returns sender, subject, date, and a short snippet for each.",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Number of emails to fetch (default 5, max 10)",
                }
            },
            "required": [],
        },
    },
    {
        "name": "search_emails",
        "description": "Search Gmail for specific emails. Supports Gmail search syntax like 'from:someone@email.com', 'subject:meeting', 'is:unread', 'newer_than:2d', etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Gmail search query",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results to return (default 5)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_todays_calendar",
        "description": "Get all of the user's calendar events for today.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_upcoming_events",
        "description": "Get the user's upcoming calendar events for the next N days.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days to look ahead (default 7)",
                }
            },
            "required": [],
        },
    },
]

# Map tool names → handler functions
TOOL_HANDLERS = {
    "get_recent_emails": lambda args: get_recent_emails(args.get("max_results", 5)),
    "search_emails": lambda args: search_emails(args["query"], args.get("max_results", 5)),
    "get_todays_calendar": lambda args: get_todays_calendar(),
    "get_upcoming_events": lambda args: get_upcoming_events(args.get("days", 7)),
}


# ---------------------------------------------------------------------------
# Claude conversation handler (with tool-use loop)
# ---------------------------------------------------------------------------

def build_messages_from_history(conversation_history: list) -> list:
    """
    Convert AgentPhone recentHistory into Claude message format.
    AgentPhone sends either:
      - {role: "user"/"agent", content: "..."} (voice)
      - {content, direction: "inbound"/"outbound", channel, at} (SMS)
    Claude expects alternating user/assistant messages.
    """
    messages = []
    for entry in (conversation_history or [])[-6:]:
        content = entry.get("content", "")
        if not content:
            continue

        # Map to Claude roles — handle both formats
        raw_role = entry.get("role", "")
        direction = entry.get("direction", "")
        if raw_role in ("agent", "assistant") or direction == "outbound":
            role = "assistant"
        else:
            role = "user"

        # Avoid consecutive same-role messages (Claude requires alternation)
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += " " + content
        else:
            messages.append({"role": role, "content": content})

    return messages


def _looks_like_tool_request(message: str) -> bool:
    """Quick check if the user is asking about email/calendar (so we send tools)."""
    keywords = [
        "email", "emails", "mail", "inbox", "unread",
        "calendar", "schedule", "event", "events", "meeting",
        "appointment", "check my", "read my",
    ]
    msg_lower = message.lower()
    return any(kw in msg_lower for kw in keywords)


def _run_tool_call(user_message: str, conversation_history: list = None):
    """
    Handle a tool-calling turn synchronously (non-streaming).
    Used when we know tools are needed — we stream an interim message separately.
    Returns the final text response after tool execution.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    messages = build_messages_from_history(conversation_history)
    messages.append({"role": "user", "content": user_message})

    max_iterations = 5
    for i in range(max_iterations):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=_system_prompt(),
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            assistant_content = response.content
            messages.append({"role": "assistant", "content": assistant_content})

            tool_results = []
            for block in assistant_content:
                if block.type == "tool_use":
                    logger.info(f"Claude calling tool: {block.name}({block.input})")
                    handler = TOOL_HANDLERS.get(block.name)
                    result = handler(block.input) if handler else f"Unknown tool: {block.name}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            messages.append({"role": "user", "content": tool_results})
        else:
            text_parts = [
                block.text for block in response.content if hasattr(block, "text")
            ]
            return " ".join(text_parts)

    return "Sorry, I'm having trouble processing that right now. Can you try again?"


# Sentence boundary regex — splits on .!? followed by a space or end-of-string
_SENTENCE_RE = re.compile(r'(?<=[.!?])\s+')


def _stream_claude_chat(user_message: str, conversation_history: list = None):
    """
    Generator that yields sentences as Claude streams its response.
    Buffers tokens and flushes on sentence boundaries so AgentPhone can
    start TTS on the first sentence while Claude is still generating.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    messages = build_messages_from_history(conversation_history)
    messages.append({"role": "user", "content": user_message})

    buffer = ""
    t_start = time.time()
    first_token_logged = False
    first_sentence_logged = False

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=_system_prompt(),
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            buffer += text

            if not first_token_logged:
                logger.info(f"  TTFT (time to first token): {time.time() - t_start:.2f}s")
                first_token_logged = True

            # Flush complete sentences as interim chunks
            sentences = _SENTENCE_RE.split(buffer)
            if len(sentences) > 1:
                for sentence in sentences[:-1]:
                    sentence = sentence.strip()
                    if sentence:
                        if not first_sentence_logged:
                            logger.info(f"  TTFS (time to first sentence): {time.time() - t_start:.2f}s")
                            first_sentence_logged = True
                        yield sentence
                buffer = sentences[-1]

    # Flush any remaining text as the final chunk
    if buffer.strip():
        if not first_sentence_logged:
            logger.info(f"  TTFS (time to first sentence): {time.time() - t_start:.2f}s")
        yield buffer.strip()

    logger.info(f"  Stream complete: {time.time() - t_start:.2f}s total")


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

def verify_webhook_signature(payload_body: bytes, signature_header: str, timestamp_header: str) -> bool:
    """
    Verify AgentPhone HMAC-SHA256 webhook signature.
    Signature format: sha256=<hex>
    Signed string: <timestamp>.<body>
    """
    if not AGENTPHONE_WEBHOOK_SECRET:
        logger.warning("No webhook secret set — skipping signature check")
        return True

    if not signature_header:
        return False

    # Reject requests older than 5 minutes
    if timestamp_header:
        try:
            ts = int(timestamp_header)
            if abs(time.time() - ts) > 300:
                logger.warning("Webhook timestamp too old")
                return False
        except ValueError:
            pass

    # Build signed string: timestamp.body
    signed_payload = payload_body
    if timestamp_header:
        signed_payload = f"{timestamp_header}.".encode("utf-8") + payload_body

    expected = "sha256=" + hmac.new(
        AGENTPHONE_WEBHOOK_SECRET.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature_header)


# ---------------------------------------------------------------------------
# Webhook endpoint — AgentPhone sends events here
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    """
    Receives agent.message events from AgentPhone.
    Both SMS and voice calls arrive as agent.message with different channels.

    For voice: returns streaming ndjson when tools are needed,
    so the caller hears "Let me check..." immediately.
    """
    # Verify signature
    raw_body = request.get_data()
    signature = request.headers.get("X-Webhook-Signature", "")
    timestamp = request.headers.get("X-Webhook-Timestamp", "")
    if not verify_webhook_signature(raw_body, signature, timestamp):
        logger.warning("Invalid webhook signature")
        return jsonify({"error": "Invalid signature"}), 401

    t_received = time.time()

    payload = request.get_json()
    if not payload:
        return jsonify({"error": "Empty payload"}), 400

    event_type = payload.get("event", "")
    channel = payload.get("channel", "unknown")
    data = payload.get("data", {})
    call_id = data.get("callId", "unknown")

    # SMS uses data.message, voice uses data.transcript
    if channel == "voice":
        user_message = data.get("transcript", "")
    else:
        user_message = data.get("message", "")

    # Guard: AgentPhone sometimes sends a list (conversation history) instead of a string
    if not isinstance(user_message, str):
        logger.warning(f"Unexpected transcript type: {type(user_message)}, skipping")
        return jsonify({"text": "I didn't catch that. Could you say that again?"}), 200

    conversation_history = payload.get("recentHistory", [])

    # ── AgentPhone timing analysis ──
    ap_timestamp = payload.get("timestamp", "")
    ap_event_time = None
    if ap_timestamp:
        try:
            # Parse AgentPhone's event timestamp
            ap_event_time = datetime.fromisoformat(ap_timestamp.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            pass

    # How long ago did AgentPhone say this event happened?
    ap_to_server_delay = f"{t_received - ap_event_time:.2f}s" if ap_event_time else "?"

    # How long since we last responded to this call? (AgentPhone's TTS + user thinking + STT pipeline)
    call_state = _call_timings.get(call_id, {})
    last_response_at = call_state.get("last_response_at")
    turn = call_state.get("turn", 0) + 1
    if last_response_at:
        agentphone_pipeline = f"{t_received - last_response_at:.2f}s"
    else:
        agentphone_pipeline = "first turn"

    logger.info(
        f"⏱ TIMING [{call_id[-8:]}] turn={turn} | "
        f"AgentPhone→server: {ap_to_server_delay} | "
        f"Since last response: {agentphone_pipeline} | "
        f"Transcript: \"{user_message[:60]}\""
    )

    if not user_message:
        logger.warning(f"No message found in payload. Event type: {event_type}")
        return jsonify({"text": "I didn't catch that. Could you say that again?"}), 200

    # Detect if user wants to end the call
    goodbye_phrases = ["bye", "goodbye", "hang up", "end the call", "talk to you later", "gotta go", "see you"]
    should_hangup = any(phrase in user_message.lower() for phrase in goodbye_phrases)

    # Detect if user wants to transfer the call
    transfer_to = None
    transfer_name = None
    msg_lower = user_message.lower()
    transfer_phrases = ["transfer", "connect me", "put me through", "patch me through", "forward"]
    if any(phrase in msg_lower for phrase in transfer_phrases):
        # Check canonical names
        for name, number in TRANSFER_CONTACTS.items():
            if name.lower() in msg_lower:
                transfer_to = number
                transfer_name = name
                break
        # Check STT aliases (mano → manav, etc.)
        if not transfer_to:
            for alias, canonical in TRANSFER_ALIASES.items():
                if alias in msg_lower:
                    transfer_to = TRANSFER_CONTACTS[canonical]
                    transfer_name = canonical
                    break
        if transfer_to:
            logger.info(f"[{call_id[-8:]}] Transfer requested to {transfer_name} ({transfer_to})")

    use_tools = _looks_like_tool_request(user_message)
    logger.info(f"[{call_id[-8:]}] tools_hint={use_tools} for \"{user_message[:40]}\"")

    # Update turn counter immediately (not inside generator) so overlapping
    # webhooks see the correct turn number
    _call_timings[call_id] = {"last_response_at": time.time(), "turn": turn}

    if channel == "voice":
        # Transfer: skip Claude, respond immediately with transfer signal
        if transfer_to:
            logger.info(f"[{call_id[-8:]}] Sending transfer response to {transfer_name}")
            return jsonify({
                "text": f"Transferring you to {transfer_name} now.",
                "transfer": True,
            })

        # Always stream ndjson for voice — AgentPhone starts TTS on first chunk
        def generate():
            t0 = time.time()
            chunk_count = 0
            full_response = ""

            try:
                if use_tools:
                    # Tool path: interim message → run tools → stream final answer
                    yield json.dumps({"text": "Let me check that for you. ", "interim": True}) + "\n"
                    chunk_count += 1

                    final_text = _run_tool_call(user_message, conversation_history)
                    full_response = final_text
                    yield json.dumps({"text": final_text, "hangup": should_hangup}) + "\n"
                    chunk_count += 1
                else:
                    # Chat path: stream sentence-by-sentence from Claude
                    # Each sentence is yielded as it completes, so AgentPhone
                    # can start TTS on the first sentence immediately
                    pending = None
                    for sentence in _stream_claude_chat(user_message, conversation_history):
                        # Emit the *previous* sentence as interim
                        if pending is not None:
                            full_response += pending + " "
                            yield json.dumps({"text": pending + " ", "interim": True}) + "\n"
                            chunk_count += 1
                        pending = sentence

                    # Last sentence is the final chunk (not interim)
                    if pending is not None:
                        full_response += pending + " "
                        yield json.dumps({"text": pending + " ", "hangup": should_hangup}) + "\n"
                        chunk_count += 1
            except Exception as e:
                logger.error(f"Claude error: {e}")
                full_response = "Sorry, I ran into a problem. Try again in a moment."
                yield json.dumps({"text": full_response, "hangup": should_hangup}) + "\n"
                chunk_count += 1

            elapsed = time.time() - t0
            logger.info(
                f"⏱ RESPONSE [{call_id[-8:]}] turn={turn} | "
                f"Claude: {elapsed:.2f}s (tools={use_tools}, chunks={chunk_count}) | "
                f"Response ({len(full_response)} chars): \"{full_response[:80]}\""
            )
            _call_timings[call_id] = {"last_response_at": time.time(), "turn": turn}

        return Response(
            generate(),
            content_type="application/x-ndjson",
            status=200,
        )
    else:
        # SMS — synchronous response (no streaming needed)
        try:
            t0 = time.time()
            if use_tools:
                response_text = _run_tool_call(user_message, conversation_history)
            else:
                response_text = " ".join(_stream_claude_chat(user_message, conversation_history))
            elapsed = time.time() - t0
            logger.info(f"⏱ SMS [{call_id[-8:]}] Claude: {elapsed:.2f}s | \"{response_text[:80]}\"")
        except Exception as e:
            logger.error(f"Claude error: {e}")
            response_text = "Sorry, I ran into a problem. Try again in a moment."
        _call_timings[call_id] = {"last_response_at": time.time(), "turn": turn}
        return jsonify({"text": response_text}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "agentphone-assistant"})


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        logger.error("Set ANTHROPIC_API_KEY in your .env file!")
        exit(1)

    logger.info("Starting AgentPhone Personal Assistant...")
    logger.info("Webhook: POST /webhook")
    logger.info("Health:  GET  /health")

    # Dev mode. In production use: gunicorn server:app --bind 0.0.0.0:8000
    app.run(host="0.0.0.0", port=8000, debug=True)
