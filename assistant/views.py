# assistant/views.py
#
# Stateless chat proxy: the frontend owns and sends the full message history
# (from IndexedDB) on every request. This view runs Claude's agentic
# tool-use loop internally — possibly several Claude round-trips per HTTP
# request — and returns only the final reply plus a summary of any actions
# taken. Nothing is persisted server-side in Phase 1.

import json
import logging
from datetime import date

import anthropic
from django.conf import settings
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import EncryptionKey, SyncedMessage
from .serializers import EncryptionKeySerializer, SyncedMessageSerializer
from .tools import TOOL_HANDLERS, TOOL_SCHEMAS, MUTATING_TOOLS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the personal finance assistant built into ExpenseIQ, a personal budgeting and debt-payoff app.

Today's date is {today}. Convert the user's relative dates ("today", "yesterday", "last Friday") to real ISO dates using this.

You can both answer questions AND take actions directly using your tools. The user wants you to act, not just describe what they could do manually — when they say something like "I spent $40 on groceries today", "I did laundry, $15", or "add my Visa, $2000 at 22%, minimum $60", call the right tool immediately rather than telling them to go add it themselves. Only ask a clarifying question when a required detail is genuinely missing or ambiguous (e.g. "update my card balance" when they have three cards on file).

Guidelines:
- Parse casual amounts ("40 bucks", "$1,200") into plain numbers.
- Pick the closest matching category yourself; don't make the user choose from a list unless it's truly unclear.
- If a tool reports no match or multiple matches for something (an expense, debt, or checklist item), relay that to the user plainly and ask for the detail needed — never guess an ID.
- Call get_financial_summary when you need context to answer a question (e.g. "how much debt do I have left?", "am I on track?").
- After taking an action, confirm briefly in plain language what you did — don't dump raw JSON or field names at the user.
- Be concise and warm, like a competent assistant who respects the user's time.
"""

MAX_TOOL_ROUNDS = 6


class ChatView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not settings.ANTHROPIC_API_KEY:
            return Response(
                {
                    "error": (
                        "The assistant isn't set up yet. Add ANTHROPIC_API_KEY to the backend "
                        ".env (get one at console.anthropic.com) to enable it."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        raw_messages = request.data.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            return Response({"error": "messages must be a non-empty list."}, status=status.HTTP_400_BAD_REQUEST)

        # Only trust role/content from the client — never anything else.
        messages = []
        for m in raw_messages:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            content = m.get("content")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

        if not messages:
            return Response({"error": "messages must be a non-empty list."}, status=status.HTTP_400_BAD_REQUEST)

        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        system_prompt = SYSTEM_PROMPT.format(today=date.today().isoformat())
        actions = []

        try:
            for _ in range(MAX_TOOL_ROUNDS):
                response = client.messages.create(
                    model=settings.ANTHROPIC_MODEL,
                    max_tokens=1024,
                    system=system_prompt,
                    tools=TOOL_SCHEMAS,
                    messages=messages,
                )

                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason != "tool_use":
                    reply_text = "".join(
                        block.text for block in response.content if block.type == "text"
                    ).strip()
                    return Response({"reply": reply_text, "actions": actions})

                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue

                    handler = TOOL_HANDLERS.get(block.name)
                    if handler is None:
                        result = {"ok": False, "message": f"Unknown tool '{block.name}'."}
                    else:
                        try:
                            result = handler(request.user, **(block.input or {}))
                        except Exception:
                            logger.exception("Assistant tool '%s' failed", block.name)
                            result = {"ok": False, "message": "Something went wrong running that action. Please try again."}

                    if block.name in MUTATING_TOOLS and result.get("ok"):
                        actions.append({
                            "tool": block.name,
                            "input": block.input,
                            "message": result.get("message", ""),
                        })

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                    })

                messages.append({"role": "user", "content": tool_results})

            return Response({
                "reply": (
                    "I took a few actions but this is going on longer than expected — "
                    "could you check what happened and let me know what's still needed?"
                ),
                "actions": actions,
            })

        except anthropic.APIError:
            logger.exception("Anthropic API error in assistant chat")
            return Response(
                {"error": "The assistant is temporarily unavailable. Please try again in a moment."},
                status=status.HTTP_502_BAD_GATEWAY,
            )


# ─────────────────────────────────────────────
# Phase 2: optional cross-device sync
#
# Both views only ever handle opaque, client-encrypted blobs — nothing
# here decrypts or inspects message content or the DEK. See models.py.
# ─────────────────────────────────────────────

class EncryptionKeyView(APIView):
    """
    GET  /api/assistant/encryption-key/  → the wrapped DEK for this user,
         or 404 if no device has enabled sync yet.
    PUT  /api/assistant/encryption-key/  → set/replace it — the first
         device to enable sync creates it; a passphrase change re-wraps
         and overwrites it.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            key = EncryptionKey.objects.get(user=request.user)
        except EncryptionKey.DoesNotExist:
            return Response({"error": "No sync key set up yet."}, status=status.HTTP_404_NOT_FOUND)
        return Response(EncryptionKeySerializer(key).data)

    def put(self, request):
        key, _ = EncryptionKey.objects.get_or_create(user=request.user)
        serializer = EncryptionKeySerializer(key, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SyncMessagesView(APIView):
    """
    GET    /api/assistant/sync/messages/?since=<ISO timestamp>  → messages
           created after `since` (omit for the full history).
    POST   /api/assistant/sync/messages/  → upsert a batch:
           {"messages": [{"client_id", "ciphertext", "iv"}, ...]}
    DELETE /api/assistant/sync/messages/  → wipe this user's synced
           history (used when they start a new conversation).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = SyncedMessage.objects.filter(user=request.user)
        since = request.query_params.get("since")
        if since:
            qs = qs.filter(created_at__gt=since)
        return Response(SyncedMessageSerializer(qs, many=True).data)

    def post(self, request):
        items = request.data.get("messages")
        if not isinstance(items, list):
            return Response({"error": "messages must be a list."}, status=status.HTTP_400_BAD_REQUEST)

        saved = []
        for item in items:
            if not isinstance(item, dict):
                continue
            client_id  = item.get("client_id")
            ciphertext = item.get("ciphertext")
            iv         = item.get("iv")
            if not (client_id and ciphertext and iv):
                continue
            obj, _ = SyncedMessage.objects.update_or_create(
                user=request.user, client_id=client_id,
                defaults={"ciphertext": ciphertext, "iv": iv},
            )
            saved.append(obj)

        return Response(SyncedMessageSerializer(saved, many=True).data, status=status.HTTP_201_CREATED)

    def delete(self, request):
        SyncedMessage.objects.filter(user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
