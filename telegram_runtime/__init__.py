"""AutoTelegramForward — Python Telegram Runtime.

The narrow, specialized Telegram connectivity and execution adapter the Go
Worker talks to over the versioned ``TelegramGateway`` gRPC contract.

Scope (what Python owns here, and nothing else):

* Telegram session lifecycle — creation, restoration, validation, reconnection.
* MTProto request execution through Pyrogram.
* Receiving Telegram updates and normalizing them into application events.
* Telegram-specific message retrieval, sending, forwarding, copying.
* Telegram-specific media download / upload / album operations.
* Telegram RPC error normalization into the shared typed vocabulary.

Explicitly NOT owned (Go owns it):

* Business routing, rule evaluation, forwarding decisions.
* Retry orchestration, application-level scheduling, durable job lifecycle.
* Queue business semantics, business-level delivery status.

Go decides WHAT should happen; this runtime executes the Telegram-specific
operation Go asked for and reports WHAT Telegram actually reported.

The runtime is started from the existing Core process (``-m
telegram_runtime`` style) so a deployment does not gain a new service to
operate until the extraction is complete; it binds its own gRPC port
alongside the Core's control-plane port.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
