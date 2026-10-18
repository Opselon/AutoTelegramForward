"""TaskControlServicer: the gRPC surface for the per-user task lifecycle.

Identity is NOT trusted from the request body: ``owner_user_id`` is stamped
by the Go API from the verified JWT, and anything else fails closed exactly
like the other servicers. A foreign task id is deliberately the same shape
as a nonexistent one.
"""

from __future__ import annotations

import logging

from ...proto import autoforward_pb2 as pb
from ...proto import autoforward_pb2_grpc as pb_grpc
from ...application.user_tasks import TaskError

logger = logging.getLogger(__name__)


def _task_to_pb(task) -> pb.UserTask:
    return pb.UserTask(
        id=task.id,
        owner_user_id=int(task.owner_user_id),
        kind=task.kind,
        status=task.status,
        idempotency_key=task.idempotency_key,
        attempt=int(task.attempt),
        max_attempts=int(task.max_attempts),
        payload_json=task.payload_json,
        result_json=task.result_json,
        error=task.error,
        error_code=task.error_code,
        created_at=int(task.created_at),
        updated_at=int(task.updated_at),
        started_at=int(task.started_at),
        finished_at=int(task.finished_at),
    )


class TaskControlServicer(pb_grpc.TaskControlServiceServicer):
    def __init__(self, tasks) -> None:
        # `tasks` is the application UserTaskUseCases instance.
        self._tasks = tasks

    @staticmethod
    def _owner(request) -> int:
        # Fail-closed: the API resolves the JWT owner; only that value counts.
        return int(getattr(request, "owner_user_id", 0) or 0)

    async def CreateTask(self, request, context):
        try:
            task, created = await self._tasks.create(
                self._owner(request),
                request.kind,
                payload_json=request.payload_json,
                idempotency_key=request.idempotency_key,
                max_attempts=request.max_attempts,
            )
            return pb.CreateTaskResponse(success=True, task=_task_to_pb(task),
                                         already_existed=not created)
        except TaskError as exc:
            return pb.CreateTaskResponse(success=False, error=str(exc), error_code=exc.code)

    async def GetTask(self, request, context):
        if not self._owner(request):
            return pb.TaskResponse(success=False, error="unauthorized",
                                   error_code="unauthorized")
        task = await self._tasks.get_owned(request.task_id, self._owner(request))
        if not task:
            return pb.TaskResponse(success=False, error="not_found",
                                   error_code="not_found")
        return pb.TaskResponse(success=True, task=_task_to_pb(task))

    async def ListTasks(self, request, context):
        try:
            tasks, total = await self._tasks.list_for_owner(
                self._owner(request), status=request.status, kind=request.kind,
                limit=request.limit or 50, offset=request.offset)
        except TaskError as exc:
            return pb.ListTasksResponse(success=False, error=str(exc), error_code=exc.code)
        if not self._owner(request):
            return pb.ListTasksResponse(success=False, error="unauthorized",
                                        error_code="unauthorized")
        return pb.ListTasksResponse(
            success=True, tasks=[_task_to_pb(t) for t in tasks], total=int(total))

    async def CancelTask(self, request, context):
        if not self._owner(request):
            return pb.TaskResponse(success=False, error="unauthorized",
                                   error_code="unauthorized")
        try:
            task = await self._tasks.cancel_owned(request.task_id, self._owner(request))
            return pb.TaskResponse(success=True, task=_task_to_pb(task))
        except TaskError as exc:
            return pb.TaskResponse(success=False, error=str(exc), error_code=exc.code)

    async def RetryTask(self, request, context):
        if not self._owner(request):
            return pb.TaskResponse(success=False, error="unauthorized",
                                   error_code="unauthorized")
        try:
            task = await self._tasks.retry_owned(request.task_id, self._owner(request))
            return pb.TaskResponse(success=True, task=_task_to_pb(task))
        except TaskError as exc:
            return pb.TaskResponse(success=False, error=str(exc), error_code=exc.code)
