"""ReplaceRuleControlServicer — the gRPC surface of the replace-rule manager.

The manager itself lives in :mod:`core.application.replace_rules`; this file is
only transport. Two rules keep it that way:

* **No logic here.** Every decision (what a valid pattern is, which tasks a rule
  may be assigned to, what a page contains) belongs to the service, so the bot,
  the dashboard and the mobile client cannot disagree by going through different
  code. The servicer converts protobuf to a dict, calls the service, converts
  the answer back.
* **Ownership is not negotiable.** ``owner_user_id`` is resolved from the
  authenticated principal by the Go API and passed through untouched. Zero is
  refused rather than read as "all", so a caller that forgets it gets nothing
  instead of everything.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import grpc

from ...application.replace_rules import (
    ReplaceRuleError,
    ReplaceRuleService,
    error_key,
)
from ...proto import autoforward_pb2 as pb
from ...proto import autoforward_pb2_grpc as pb_grpc

logger = logging.getLogger("atf.grpc.replace")

#: A refusal that names the missing row is NOT_FOUND; everything else is the
#: caller's payload, so INVALID_ARGUMENT.
_CODE_TO_STATUS = {
    "not_found": grpc.StatusCode.NOT_FOUND,
    "task_not_found": grpc.StatusCode.INVALID_ARGUMENT,
    "task_required": grpc.StatusCode.INVALID_ARGUMENT,
    "no_tasks": grpc.StatusCode.FAILED_PRECONDITION,
    "too_many_entries": grpc.StatusCode.FAILED_PRECONDITION,
    "invalid_entry": grpc.StatusCode.INVALID_ARGUMENT,
}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _rule_pb(view: Any) -> Any:
    return pb.ReplaceRule(
        id=view.id,
        name=view.name,
        source=view.source,
        to=view.to,
        mode=view.mode,
        flags=view.flags,
        whole_word=bool(view.whole_word),
        enabled=bool(view.enabled),
        priority=int(view.priority),
        task_ids=list(view.task_ids),
        task_names=list(view.task_names),
        version=int(view.version),
        updated_at=int(view.updated_at),
        badge=view.badge,
    )


class ReplaceRuleControlServicer(pb_grpc.ReplaceRuleControlServiceServicer):
    def __init__(self, service: ReplaceRuleService) -> None:
        self._service = service

    # -- plumbing ---------------------------------------------------------
    async def _abort(self, context: Any, exc: ReplaceRuleError) -> None:
        status = _CODE_TO_STATUS.get(exc.code, grpc.StatusCode.INVALID_ARGUMENT)
        # The code travels in the message so the Go API can answer with a stable
        # machine-readable body instead of parsing a human sentence.
        detail = exc.detail or exc.code
        await context.abort(status, f"{exc.code}: {detail}")

    @staticmethod
    def _owner(request: Any) -> int:
        return _int(getattr(request, "owner_user_id", 0), 0)

    @staticmethod
    def _input(request: Any) -> Dict[str, Any]:
        """The protobuf input as a dict, with presence preserved.

        ``HasField`` is what makes a partial update partial: a field the client
        did not send is absent from the dict, so the service merges onto the
        stored rule instead of writing an empty string over it.
        """
        rule = getattr(request, "rule", None)
        if rule is None:
            return {}
        out: Dict[str, Any] = {}
        for field in ("name", "source", "to", "mode", "flags"):
            if rule.HasField(field):
                out[field] = getattr(rule, field)
        for field in ("whole_word", "enabled", "priority"):
            if rule.HasField(field):
                out[field] = getattr(rule, field)
        task_ids = list(getattr(rule, "task_ids", []) or [])
        if task_ids:
            out["task_ids"] = task_ids
        return out

    # -- reads ------------------------------------------------------------
    async def ListReplaceRules(self, request, context):
        owner = self._owner(request)
        if not owner:
            # Fail closed, and in the shape the client already renders: an empty
            # page with zero counts is a truthful answer to "nothing for you".
            return pb.ListReplaceRulesResponse(
                page=1, per_page=0, total=0, pages=1,
                counts=pb.ReplaceRuleCounts(), status="all",
            )
        page = await self._service.list(
            owner,
            query=request.query,
            status=request.status,
            task_id=request.task_id,
            page=_int(request.page, 1) or 1,
            per_page=_int(request.per_page, 0) or 20,
        )
        return pb.ListReplaceRulesResponse(
            rules=[pb.ReplaceRule(**item) for item in page["items"]],
            page=page["page"],
            per_page=page["per_page"],
            total=page["total"],
            pages=page["pages"],
            counts=pb.ReplaceRuleCounts(**page["counts"]),
            query=page["query"],
            status=page["status"],
            task_id=page["task_id"],
            tasks=[pb.ReplaceRuleTask(**task) for task in page["tasks"]],
        )

    async def GetReplaceRule(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "owner_user_id is required")
        try:
            view = await self._service.get(owner, request.rule_id)
        except ReplaceRuleError as exc:
            await self._abort(context, exc)
            return pb.ReplaceRule()
        return _rule_pb(view)

    async def ListReplaceRuleTasks(self, request, context):
        owner = self._owner(request)
        if not owner:
            return pb.ListReplaceRuleTasksResponse()
        tasks = await self._service.tasks(owner)
        return pb.ListReplaceRuleTasksResponse(
            tasks=[pb.ReplaceRuleTask(**task) for task in tasks]
        )

    # -- writes -----------------------------------------------------------
    async def CreateReplaceRule(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "owner_user_id is required")
        try:
            view = await self._service.create(owner, self._input(request))
        except ReplaceRuleError as exc:
            await self._abort(context, exc)
            return pb.ReplaceRule()
        return _rule_pb(view)

    async def UpdateReplaceRule(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "owner_user_id is required")
        try:
            view = await self._service.update(owner, request.rule_id, self._input(request))
        except ReplaceRuleError as exc:
            await self._abort(context, exc)
            return pb.ReplaceRule()
        return _rule_pb(view)

    async def DeleteReplaceRule(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "owner_user_id is required")
        try:
            result = await self._service.delete(owner, request.rule_id)
        except ReplaceRuleError as exc:
            await self._abort(context, exc)
            return pb.DeleteReplaceRuleResponse()
        return pb.DeleteReplaceRuleResponse(ok=bool(result.get("ok")),
                                            removed=_int(result.get("removed")))

    async def SetReplaceRuleEnabled(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "owner_user_id is required")
        try:
            # Absolute, not a toggle: a retried call (a double tap, a retry after
            # a timeout) must land on the state the user asked for, not flip it
            # back.
            view = await self._service.update(
                owner, request.rule_id, {"enabled": bool(request.enabled)}
            )
        except ReplaceRuleError as exc:
            await self._abort(context, exc)
            return pb.ReplaceRule()
        return _rule_pb(view)

    async def DuplicateReplaceRule(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "owner_user_id is required")
        try:
            view = await self._service.duplicate(owner, request.rule_id, request.name)
        except ReplaceRuleError as exc:
            await self._abort(context, exc)
            return pb.ReplaceRule()
        return _rule_pb(view)

    async def TestReplaceRule(self, request, context):
        owner = self._owner(request)
        if not owner:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "owner_user_id is required")
        payload: Dict[str, Any] = self._input(request)
        payload["sample"] = request.sample
        if request.rule_id:
            payload["rule_id"] = request.rule_id
        try:
            result = await self._service.test(owner, payload)
        except ReplaceRuleError as exc:
            await self._abort(context, exc)
            return pb.TestReplaceRuleResponse()
        return pb.TestReplaceRuleResponse(
            ok=bool(result.get("ok")),
            input=result.get("input", ""),
            output=result.get("output", ""),
            matches=_int(result.get("matches")),
            changed=bool(result.get("changed")),
        )


__all__ = ["ReplaceRuleControlServicer", "error_key", "ReplaceRuleService"]
