"""Auto-reply application layer.

Import-safe facade over the pieces:

* :mod:`.context`    — prompt assembly (persona, tone, language, memory, depth)
* :mod:`.targeting`  — who is eligible for a reply
* :mod:`.engine`     — the decision path that runs on the inbound hot path
* :mod:`.worker`     — the durable queue consumer that generates and sends
* :mod:`.service`    — tenant-facing operations shared by every surface
"""

from .context import PromptContext, build_messages, build_system_prompt, resolve_language
from .engine import AutoReplyDecision, AutoReplyEngine, decide
from .service import AutoReplyError, AutoReplyService
from .targeting import is_targeted
from .worker import AutoReplyWorker, OutboundSender

__all__ = [
    "AutoReplyDecision",
    "AutoReplyEngine",
    "AutoReplyError",
    "AutoReplyService",
    "AutoReplyWorker",
    "OutboundSender",
    "PromptContext",
    "build_messages",
    "build_system_prompt",
    "decide",
    "is_targeted",
    "resolve_language",
]
