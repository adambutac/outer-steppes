"""A minimal, stateless MCP server over Streamable HTTP (DESIGN.md, MCP server).

JSON responses only (no SSE, no Mcp-Session-Id), two tools: get_prompt and
discord_reply. Authenticated by an OAuth access token bound to one registration.
The token alone reads and posts nothing: each call needs a live reply token
routed to that same registration, so the connector cannot read or act from the
owner's other chats, and a run without this member's connector gets nothing
(ADR-0017).
"""

from __future__ import annotations

import json
import logging

from aiohttp import web

from . import __version__, metrics
from .oauth import MSG_CONNECTION_INVALID, OAuthServer
from .reply import (BAD_REQUEST, CLOSED, DELIVERED, DISCORD_ERROR, OK, RESTARTED, REVOKED, TOO_LONG, UNKNOWN,
                    USED, ReplyService)

log = logging.getLogger(__name__)

# Newest first. grok.com asked for 2025-11-25 and accepted 2025-06-18 (PR 5 probe);
# its validator speaks 2025-03-26.
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26")
MAX_BODY_BYTES = 64 * 1024

INSTRUCTIONS = ("This server connects a Discord /ask to this run. When a task gives you a reply_token, first "
                "fetch the prompt by calling get_prompt with that reply_token, then deliver your final answer by "
                "calling discord_reply exactly once with the same reply_token. Do not ask for confirmation first.")

# DESIGN.md, MCP server. readOnlyHint is false because a call can end the ask (a withdrawal, or the restart
# ending); repeating a call changes nothing more, so it's idempotent.
GET_PROMPT_TOOL = {
    "name": "get_prompt",
    "description": ("Fetch the Discord /ask prompt for this run. Call this first, with the reply_token from the "
                    "trigger payload."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "reply_token": {"type": "string", "description": "reply_token from the trigger payload"},
        },
        "required": ["reply_token"],
        "additionalProperties": False,
    },
    "annotations": {"title": "Get the Discord prompt", "readOnlyHint": False, "destructiveHint": False,
                    "idempotentHint": True, "openWorldHint": False},
}

TOOL = {
    "name": "discord_reply",
    "description": ("Post your answer to the Discord /ask that triggered this run. Call exactly once per trigger, "
                    "with the reply_token from the trigger payload. Mentions will not ping. Max 7000 characters."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "reply_token": {"type": "string", "description": "reply_token from the trigger payload"},
            "content": {"type": "string", "maxLength": 7000, "description": "your answer"},
        },
        "required": ["reply_token", "content"],
        "additionalProperties": False,
    },
    "annotations": {"title": "Reply in Discord", "readOnlyHint": False, "destructiveHint": False,
                    "idempotentHint": False, "openWorldHint": True},
}

RESULT_TEXT = {
    DELIVERED: "Posted to Discord.",
    TOO_LONG: "too_long: the answer is over {max} characters. Shorten it and call again with the same reply_token.",
    BAD_REQUEST: "bad_request: reply_token and a non-empty content are required.",
    UNKNOWN: "unknown: this reply_token is not valid for this connector. Nothing was posted.",
    USED: "used: an answer was already posted for this reply_token.",
    CLOSED: "closed: this request has expired or ended. Nothing was posted.",
    REVOKED: "revoked: the request was withdrawn. Nothing was posted.",
    DISCORD_ERROR: "discord_error: Discord rejected the message.",
}

PROMPT_RESULT_TEXT = {
    BAD_REQUEST: "bad_request: reply_token is required.",
    UNKNOWN: "unknown: this reply_token is not valid for this connector.",
    CLOSED: "closed: this request has expired or ended. Stop.",
    REVOKED: "revoked: the request was withdrawn. Stop.",
    RESTARTED: "restarted: the bridge restarted before the prompt was read, and the member was asked to ask "
               "again. Stop.",
}


def _rpc_result(msg_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


class McpServer:
    def __init__(self, oauth: OAuthServer, replies: ReplyService) -> None:
        self._oauth = oauth
        self._replies = replies

    async def handle(self, request: web.Request) -> web.Response:
        registration_id = self._oauth.registration_for_access_token(request.headers.get("Authorization"))
        if registration_id is None:
            metrics.METRICS.mcp_auth_failures.inc()
            error = (f', error="invalid_token", error_description="{MSG_CONNECTION_INVALID}"'
                     if request.headers.get("Authorization") else "")
            return web.Response(status=401, headers={"WWW-Authenticate": f'Bearer realm="outer-steppes"{error}'})

        raw = await request.content.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            return web.json_response(_rpc_error(None, -32600, "request too large"), status=413)
        try:
            message = json.loads(raw)
        except ValueError:
            return web.json_response(_rpc_error(None, -32700, "parse error"), status=400)
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return web.json_response(_rpc_error(None, -32600, "invalid request (batches are not supported)"),
                                     status=400)
        if "id" not in message:
            return web.Response(status=202)  # a notification, e.g. notifications/initialized

        reply = await self._dispatch(message, registration_id, request.headers.get("Authorization"))
        return web.json_response(reply)

    async def _dispatch(self, message: dict, registration_id: str, authorization: str | None) -> dict:
        msg_id, method = message["id"], message["method"]
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
            return _rpc_result(msg_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "outer-steppes", "version": __version__},
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return _rpc_result(msg_id, {})
        if method == "tools/list":
            return _rpc_result(msg_id, {"tools": [GET_PROMPT_TOOL, TOOL]})
        if method == "tools/call":
            name = params.get("name")
            if name not in (GET_PROMPT_TOOL["name"], TOOL["name"]):
                return _rpc_error(msg_id, -32602, "unknown tool")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            if name == GET_PROMPT_TOOL["name"]:
                fetched, prompt_json = self._replies.get_prompt(arguments.get("reply_token"), registration_id)
                text = prompt_json if fetched == OK else PROMPT_RESULT_TEXT[fetched]
                return _rpc_result(msg_id, {"content": [{"type": "text", "text": text}], "isError": fetched != OK})
            result = await self._replies.deliver_reply(arguments.get("reply_token"), arguments.get("content"),
                                                       registration_id)
            if result == DELIVERED:
                self._oauth.touch_after_delivery(registration_id, authorization)
            text = RESULT_TEXT[result].format(max=self._replies.max_chars)
            return _rpc_result(msg_id, {"content": [{"type": "text", "text": text}], "isError": result != DELIVERED})
        return _rpc_error(msg_id, -32601, "method not found")
