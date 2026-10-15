"""gRPC servicer for administrative control plane and client self-management."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Optional

from ...proto import autoforward_pb2 as pb
from ...proto import autoforward_pb2_grpc as pb_grpc

logger = logging.getLogger("atf.admin_servicer")


class AdminControlServicer(pb_grpc.AdminControlServiceServicer):
    """Servicer implementing AdminControlService for both Admin and Client Control Planes."""

    def __init__(
        self,
        db,
        users,
        rules,
        admin_service,
        credentials=None,
        pool=None,
        error_log=None,
        queue_manager=None,
        bot=None,
        web_url: str = "",
        i18n=None,
    ) -> None:
        self._db = db
        self._users = users
        self._rules = rules
        self._admin_service = admin_service
        self._credentials = credentials
        self._pool = pool
        self._error_log = error_log
        self._queue_manager = queue_manager
        self._bot = bot
        self._web_url = web_url
        # Localization catalog: background notifications to a user must
        # render in THAT user's language, never the process-wide default.
        self._i18n = i18n
        self._start_time = int(time.time())

    def _localized(self, key: str, uid: int, **kw) -> str:
        """Render a background notification in the recipient's own language.

        Falls back to English when no catalog is wired or the recipient has
        no stored preference. Never reads a process-wide language singleton:
        a background job must not inherit whichever language the last
        interactive user happened to pick.
        """
        if self._i18n is None:
            return key
        try:
            table = getattr(self._i18n, "_catalog", {}).get(self._i18n_lang(uid)) or {}
            template = table.get(key)
            if template is None:
                return key
            return template.format(**kw) if kw else template
        except Exception:
            return key

    def _i18n_lang(self, uid: int) -> str:
        """The recipient's persisted language preference, or English."""
        try:
            row = self._db.query_one(
                "SELECT language FROM users WHERE user_id=?", (int(uid),))
            if row and row.get("language"):
                code = str(row["language"]).lower()
                if code in ("en", "fa", "ru", "zh", "pt", "uz", "id", "es", "fr", "it"):
                    return code
        except Exception:
            pass
        return "en"

    async def GetOverview(self, request, context):
        now = int(time.time())
        uptime = now - self._start_time
        total_users = 0
        active_rules = 0
        queue_pending = 0
        recent_errors = 0

        try:
            if self._users:
                total_users = await self._users.count_all()
        except Exception as exc:
            logger.warning("Error counting users: %s", exc)

        try:
            if self._rules:
                all_rules = await self._rules.list_all()
                active_rules = sum(1 for r in all_rules if getattr(r, "enabled", True))
        except Exception as exc:
            logger.warning("Error counting rules: %s", exc)

        try:
            if self._queue_manager and hasattr(self._queue_manager, "stats"):
                st = await self._queue_manager.stats()
                queue_pending = int(st.get("pending", 0) or 0)
        except Exception:
            pass

        try:
            if self._error_log and hasattr(self._error_log, "counts_by_severity"):
                cnts = await self._error_log.counts_by_severity()
                recent_errors = int(cnts.get("error", 0) or cnts.get("fatal", 0) or 0)
        except Exception:
            pass

        return pb.AdminOverviewResponse(
            core_online=True,
            uptime_seconds=uptime,
            total_users=total_users,
            active_rules=active_rules,
            queue_pending=queue_pending,
            recent_errors=recent_errors,
            system_status="operational" if recent_errors < 10 else "degraded",
        )

    async def ListUsers(self, request, context):
        limit = max(1, min(request.limit or 50, 200))
        offset = max(0, request.offset or 0)
        users_list = []
        total = 0

        try:
            if self._users:
                total = await self._users.count_all()
                rows = await self._users.list_all(limit=limit, offset=offset)
                all_rules = await self._rules.list_all() if self._rules else []
                rule_counts = {}
                for r in all_rules:
                    uid = getattr(r, "owner_user_id", 0)
                    rule_counts[uid] = rule_counts.get(uid, 0) + 1

                for row in rows:
                    uid = int(row.get("user_id", 0))
                    # check web_account username if present
                    uname = ""
                    try:
                        acc_row = self._db.query_one(
                            "SELECT username FROM web_accounts WHERE user_id=?", (uid,)
                        )
                        if acc_row:
                            uname = acc_row["username"] or ""
                    except Exception:
                        pass

                    is_adm = False
                    if self._admin_service:
                        is_adm = self._admin_service.is_admin(uid)
                    else:
                        is_adm = bool(row.get("is_admin", 0))

                    users_list.append(
                        pb.AdminUser(
                            user_id=uid,
                            username=uname or f"User {uid}",
                            plan=str(row.get("plan", "free")),
                            status=str(row.get("status", "active")),
                            is_admin=is_adm,
                            rules_count=rule_counts.get(uid, 0),
                            created_at=int(row.get("created_at", 0) or 0),
                        )
                    )
        except Exception as exc:
            logger.error("ListUsers failed: %s", exc)

        return pb.AdminListUsersResponse(users=users_list, total=total)

    async def SetUserStatus(self, request, context):
        uid = int(request.user_id)
        status = str(request.status).strip().lower()
        if status not in ("active", "suspended"):
            return pb.StatusResponse(success=False, message="Status must be 'active' or 'suspended'")

        try:
            if self._users:
                await self._users.set_status(uid, status)
            return pb.StatusResponse(success=True, message=f"User {uid} status set to {status}")
        except Exception as exc:
            logger.error("SetUserStatus failed for uid=%s: %s", uid, exc)
            return pb.StatusResponse(success=False, message=str(exc))

    async def Broadcast(self, request, context):
        text = (request.message_text or "").strip()
        if not text:
            return pb.AdminBroadcastResponse(success=False, message="Message text cannot be empty")

        initiator_id = int(request.initiator_user_id or 0)
        target = str(request.target or "all")
        bcast_id = str(uuid.uuid4())
        now = int(time.time())

        # Collect eligible recipients
        recipients = []
        try:
            rows = self._db.query_all(
                "SELECT user_id, plan FROM users WHERE status != 'suspended'"
            )
            for r in rows or []:
                uid = int(r["user_id"])
                uplan = str(r.get("plan", "free"))
                if target == "all" or target == uplan:
                    recipients.append(uid)
        except Exception as exc:
            logger.error("Failed querying recipients for broadcast: %s", exc)

        total_recipients = len(recipients)
        try:
            self._db.execute(
                "INSERT INTO broadcast_queue (id, initiator_user_id, message_text, target, "
                "status, total_recipients, sent_count, failed_count, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'running', ?, 0, 0, ?, ?)",
                (bcast_id, initiator_id, text, target, total_recipients, now, now),
            )
        except Exception as exc:
            logger.warning("Could not record broadcast queue in db: %s", exc)

        # Dispatch background worker without blocking
        asyncio.create_task(
            self._execute_broadcast(bcast_id, initiator_id, text, recipients)
        )

        return pb.AdminBroadcastResponse(
            success=True,
            broadcast_id=bcast_id,
            total_recipients=total_recipients,
            message="Broadcast queued successfully for delivery",
        )

    async def _execute_broadcast(
        self, bcast_id: str, initiator_id: int, text: str, recipients: list[int]
    ) -> None:
        """Rate-limited background delivery across recipients."""
        sent = 0
        failed = 0

        for uid in recipients:
            if not self._bot:
                failed += 1
                continue
            try:
                await self._bot.send_message(uid, text)
                sent += 1
            except Exception as exc:
                failed += 1
                exc_str = str(exc).lower()
                if "flood" in exc_str:
                    await asyncio.sleep(2.0)
            # 25 messages/sec max
            await asyncio.sleep(0.04)

        now = int(time.time())
        try:
            self._db.execute(
                "UPDATE broadcast_queue SET status='completed', sent_count=?, failed_count=?, "
                "updated_at=? WHERE id=?",
                (sent, failed, now, bcast_id),
            )
        except Exception:
            pass

        if self._bot and initiator_id > 0:
            try:
                msg = self._localized("ui_broadcast_done", initiator_id,
                                      sent=sent, failed=failed, total=len(recipients))
                await self._bot.send_message(initiator_id, msg)
            except Exception:
                pass

    async def GetSettings(self, request, context):
        fs_enabled = False
        fs_channel = ""
        web_url = self._web_url
        reg_open = True

        try:
            row = self._db.query_one("SELECT value FROM app_settings WHERE key='force_sub_enabled'")
            if row and row["value"] == "1":
                fs_enabled = True
            row = self._db.query_one("SELECT value FROM app_settings WHERE key='force_sub_channel'")
            if row and row["value"]:
                fs_channel = str(row["value"])
            row = self._db.query_one("SELECT value FROM app_settings WHERE key='web_url'")
            if row and row["value"]:
                web_url = str(row["value"])
            row = self._db.query_one("SELECT value FROM app_settings WHERE key='registration_open'")
            if row and row["value"] == "0":
                reg_open = False
        except Exception as exc:
            logger.warning("Error fetching settings: %s", exc)

        return pb.AdminSettingsResponse(
            force_sub_enabled=fs_enabled,
            force_sub_channel=fs_channel,
            web_url=web_url,
            registration_open=reg_open,
        )

    async def SaveSettings(self, request, context):
        now = int(time.time())
        try:
            self._db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES ('force_sub_enabled', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                ("1" if request.force_sub_enabled else "0", now),
            )
            self._db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES ('force_sub_channel', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (str(request.force_sub_channel or "").strip(), now),
            )
            if request.web_url:
                clean_url = str(request.web_url).strip().rstrip("/")
                self._web_url = clean_url
                self._db.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES ('web_url', ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (clean_url, now),
                )
            self._db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES ('registration_open', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                ("1" if request.registration_open else "0", now),
            )
            return pb.StatusResponse(success=True, message="Settings updated successfully")
        except Exception as exc:
            logger.error("SaveSettings failed: %s", exc)
            return pb.StatusResponse(success=False, message=str(exc))

    async def ListAdmins(self, request, context):
        admins = []
        if self._admin_service:
            for item in self._admin_service.list_admins():
                admins.append(
                    pb.AdminAdministrator(
                        user_id=int(item["user_id"]),
                        role=str(item.get("role", "admin")),
                        added_by=int(item.get("added_by", 0)),
                        created_at=int(item.get("created_at", 0)),
                    )
                )
        return pb.AdminListAdminsResponse(admins=admins)

    async def AddAdmin(self, request, context):
        uid = int(request.user_id)
        if uid <= 0:
            return pb.StatusResponse(success=False, message="Valid positive numeric user ID required")
        try:
            if self._admin_service:
                self._admin_service.add_admin(uid, added_by=int(request.added_by or 0))
            return pb.StatusResponse(success=True, message=f"Admin privileges granted to {uid}")
        except Exception as exc:
            logger.error("AddAdmin failed: %s", exc)
            return pb.StatusResponse(success=False, message=str(exc))

    async def RemoveAdmin(self, request, context):
        uid = int(request.user_id)
        try:
            if self._admin_service:
                self._admin_service.remove_admin(uid, removed_by=int(request.removed_by or 0))
            return pb.StatusResponse(success=True, message=f"Admin privileges revoked from {uid}")
        except Exception as exc:
            logger.error("RemoveAdmin failed: %s", exc)
            return pb.StatusResponse(success=False, message=str(exc))

    async def GetClientSettings(self, request, context):
        uid = int(request.user_id)
        plan = "free"
        quota = 0
        lang = "fa"
        has_custom = False
        api_id = 0
        masked_hash = ""

        try:
            if self._users:
                u = await self._users.get(uid)
                if u:
                    plan = str(u.get("plan", "free"))
                    quota = int(u.get("quota_forwarded", 0))
                    lang = str(u.get("language", "fa"))

            if self._credentials:
                owned = await self._credentials.list_by_owner(uid)
                if owned:
                    has_custom = True
                    first = owned[0]
                    api_id = int(first.api_id)
                    h = first.api_hash or ""
                    masked_hash = f"{h[:2]}***{h[-4:]}" if len(h) >= 8 else "***"
        except Exception as exc:
            logger.warning("GetClientSettings failed for uid=%s: %s", uid, exc)

        return pb.ClientSettingsResponse(
            user_id=uid,
            plan=plan,
            quota_forwarded=quota,
            language=lang,
            has_custom_credentials=has_custom,
            api_id=api_id,
            api_hash_masked=masked_hash,
        )

    async def SaveClientCredentials(self, request, context):
        uid = int(request.user_id)
        api_id = int(request.api_id or 0)
        api_hash = (request.api_hash or "").strip()

        if uid <= 0:
            return pb.StatusResponse(success=False, message="Invalid user id")
        if api_id <= 0:
            return pb.StatusResponse(success=False, message="API ID must be a positive integer")
        if len(api_hash) != 32:
            return pb.StatusResponse(success=False, message="API Hash must be 32 hexadecimal characters")

        try:
            if self._credentials:
                # Remove prior owned creds
                existing = await self._credentials.list_by_owner(uid)
                for c in existing:
                    try:
                        await self._credentials.delete(c.id)
                    except Exception:
                        pass
                await self._credentials.create(
                    label=f"user_{uid}_cred",
                    api_id=api_id,
                    api_hash=api_hash,
                    is_default=True,
                    owner_user_id=uid,
                )

            if self._pool:
                self._pool.register_credentials(f"user_{uid}", api_id, api_hash)

            return pb.StatusResponse(success=True, message="Credentials saved and registered securely")
        except Exception as exc:
            logger.error("SaveClientCredentials error for uid=%s: %s", uid, exc)
            return pb.StatusResponse(success=False, message=str(exc))
