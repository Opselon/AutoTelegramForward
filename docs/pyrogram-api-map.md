# Pyrogram surface map — our usage, 1:1 against the pinned v2.0.106

The pinned client is **pyrogram 2.0.106** (`requirements.txt`). Its
documented surface — every high-level method with its exact signature, the
exported types, enums, handlers and the generated error classes — is
committed as machine-readable JSON in
[`docs/pyrogram-methods-2.0.106.json`](pyrogram-methods-2.0.106.json),
built from [docs.pyrogram.org](https://docs.pyrogram.org/telegram/base/)
and verified against the v2.0.106 tag source.

`python3 scripts/audit_pyrogram_surface.py` re-checks this repository
against that JSON (stdlib-only): every call site's kwargs and every
`pyrogram.errors/enums/types` import must resolve 1:1. It runs the same
check CI runs; this document is the human-readable half of the same map.

---

## 1. High-level methods we call (verified 1:1)

| pyrogram method (category) | Where we call it | Resolved by |
| --- | --- | --- |
| `send_message` (messages) | dispatcher (PV replies), durable_queue (keyword reply, bot-routed text), branding stage 1 (text op) | live path + `SendMessage` RPC |
| `forward_messages` (messages) | dispatcher + durable_queue (DIRECT_FORWARD, branding stage 2) | live path + `ForwardMessages` RPC |
| `copy_message` (messages) | dispatcher + durable_queue (copy modes, restricted fallbacks, stage 2) | live path + `CopyMessage`/`CopyMessages` RPCs |
| `copy_media_group` (messages) | durable_queue (album copies, branding stage 1 media-group op) | live path + `CopyMediaGroup` RPC |
| `send_media_group` (messages) | durable_queue (album re-upload) | live path |
| `send_photo/video/voice/audio/animation/sticker/document` (messages) | durable_queue (re-upload per media type), runtime adapter (`SendMedia` re-upload fallback) | live path + `SendMedia` RPC |
| `get_messages` (messages) | client_pool (ingest normalization), dispatcher, runtime adapter (`GetMessage`, `GetFileExtension`) | live path + `GetMessage`/`GetFileExtension` RPCs |
| `get_media_group` (messages) | runtime adapter (`GetAlbumMessageIds`) | `GetAlbumMessageIds` RPC |
| `get_chat` (chats) | durable_queue (chat title/username snapshot) | live path |
| `get_chat_history` (chats) | discovery / `/getchannel` scan | live path |
| `get_chat_member` (chats) | access checks | live path |
| `get_dialogs` (chats) | `/getchannel -id` dialog scan, discovery | live path |
| `get_users` (users) | admin/user panels | live path |
| `get_me` (users) | client_pool readiness, `/getchannel` | live path |
| `download_media` (messages) | dispatcher + durable_queue (re-upload staging), runtime adapter (`PrepareMediaUpload`, `DownloadMedia`) | live path + `PrepareMediaUpload`/`DownloadMedia` RPCs |
| `delete_messages` (messages) | durable_queue auto-delete, scheduler | live path + `DeleteMessages` RPC |
| `edit_message_text` / `edit_message_caption` (messages) | sync-edits propagation | live path |
| `send_chat_action` (messages) | typing indicator on long sends | live path |
| `resolve_peer` (utilities) | chat id resolution for raw calls | live path |
| `send_code`, `sign_in`, `check_password`, `export_session_string` (auth) | login flow | live path |
| `invoke` (advanced) | raw updates gap-filling (`GetChannelDifference` etc.) | live path |

Constructor: `Client(name=..., api_id=..., api_hash=..., session_string=..., bot_token=..., in_memory=..., no_updates=...)` — all parameters exist in the pinned `Client.__init__`.

Imports verified 1:1: 38 error classes (from the generated error surface —
`FloodWait`, `ChatForwardsRestricted`, `ChannelPrivate`, `ChatAdminRequired`,
`ChatWriteForbidden`, `SlowmodeWait`, …), 5 enums (`ChatType`,
`MessageMediaType`, `ChatAction`, …), 17 types (`Message`, `CallbackQuery`,
`InlineKeyboardButton/Markup`, `InputMedia*`, `WebAppInfo`, …), 3 handlers
(`MessageHandler`, `EditedMessageHandler`, `DeletedMessagesHandler`), and
raw functions/types (`channels.GetFullChannel`, `channels.GetMessages`,
`messages.GetDialogs`, `updates.GetChannelDifference`, `PeerChannel`,
`PeerUser`, `MessageEmpty`, `InputMessageID`, `MessageRange`,
`ChannelMessagesFilter`, `InputPeerEmpty`) — the raw layer is generated at
release build from the pinned TL schema, and every constructor we name
exists in `compiler/api/source/*.tl` (522 functions / 1670 constructors /
432 types).

---

## 2. Go Worker resolution — every gateway RPC lands on a real call

The worker never speaks Telegram itself; each RPC of
`worker/proto/telegram_gateway.proto` resolves through the runtime's
registered handler to exactly one documented pyrogram operation:

| Gateway RPC | Runtime handler (`telegram_runtime/grpc/server.py`) | pyrogram call | Failure taxonomy |
| --- | --- | --- | --- |
| `IsSessionReady` | `_sessions.is_ready` | (pool state; no RPC) | `SESSION_UNAVAILABLE` |
| `GetMessage` | `adapter.get_message` | `get_messages(chat_id, message_ids)` | normalized `TelegramOperationError` |
| `GetAlbumMessageIds` | `adapter.get_album_message_ids` | **`get_media_group(chat_id, message_id)`** | normalized |
| `GetFileExtension` | `adapter.get_file_extension` | `get_messages` + file-name sniff | normalized |
| `DownloadMedia` | `adapter.download_media` | `download_media(message, file_name=…)` | `MEDIA_DOWNLOAD_FAILED` |
| `ReloadMedia` | `adapter.reload_media` | `get_messages` + `download_media` | `MEDIA_DOWNLOAD_FAILED` |
| `PrepareMediaUpload` | `adapter.prepare_media_upload` | `download_media` to the worker's scratch path | `MEDIA_UPLOAD_FAILED` |
| `ForwardMessages` | `adapter.forward_messages` | `forward_messages(chat_id=…, from_chat_id=…, message_ids=…)` | `FLOOD_WAIT`, `SOURCE_UNAVAILABLE`, … |
| `CopyMessage` | `adapter.copy_message` | `copy_message(chat_id=…, from_chat_id=…, message_id=…, caption=…, reply_markup=…)` | restricted-source → `NeedsReupload` |
| `CopyMessages` | `adapter.copy_messages` (per-item `copy_message`) | `copy_message` | per-item, stops on first failure |
| `CopyMediaGroup` | `adapter.copy_media_group` | `copy_media_group(chat_id=…, from_chat_id=…, message_id=…, captions=…)` | restricted-source → `NeedsReupload` |
| `SendMessage` | `adapter.send_message` | `send_message(chat_id=…, text=…)` | `TARGET_UNAVAILABLE`, … |
| `SendMedia` | `adapter.send_media` | `send_photo/video/voice/audio/animation/sticker/document(photo|…=token, caption=…)` | `MEDIA_UPLOAD_FAILED` |
| `DeleteMessages` | `adapter.delete_messages` | `delete_messages(chat_id, message_ids)` | already-absent = success |
| `BrandingStage1` | server: text/copy/media_group by `operation` | `send_message` / `copy_message` / `copy_media_group` into C | `EMPTY_TEXT`, ambiguous → job UNKNOWN |
| `Bot*` (availability/text/copy/media/delete) | `bot_transport` (our Bot-API HTTP layer) | Bot API `sendMessage`/`copyMessage`/… | bot taxonomy |

Two defects this audit found and fixed (both are regression-pinned in
`telegram_runtime/tests/test_documented_surface.py`):

1. **Album resolution used a non-existent parameter.**
   `get_album_message_ids` called `get_messages(chat_id, limit=40)` — the
   pinned `get_messages` has no `limit` (that is a Bot-API concept), so
   every worker album delivery failed the resolver and degraded to a single
   copy. It now uses the documented one-to-one method,
   `get_media_group(chat_id, message_id)`, which answers exactly this
   question.
2. **Topic targeting crashed on the pinned surface.** `message_thread_id`
   does not exist on the pinned `forward_messages` / `copy_message` /
   `copy_media_group` / `send_message` / `send_*` signatures (it is a Bot-API
   parameter official pyrogram's MTProto layer never grew), so a task pointed
   at a forum topic failed every delivery with a `TypeError`. The adapter,
   the durable queue and the dispatcher now probe the installed client's
   signature once (`core/infrastructure/telegram/pyrogram_compat.py`) and
   thread the kwarg only when the method actually accepts it — on the pin a
   topic task delivers to the chat's General topic; on a fork that grew the
   parameter, topic targeting works unchanged.

---

## 3. Re-auditing

```bash
python3 scripts/audit_pyrogram_surface.py
# → 125 call site(s), 130 symbol import(s) checked against pyrogram 2.0.106
#   PASS: every pyrogram call and import resolves 1:1 against the pinned surface
```

When the pin moves (e.g. to a fork that grew `message_thread_id`), regenerate
the surface JSON from the new tag the same way this one was built — the
`$schema` key documents the shape — and re-run the audit; the compat guards
pick the new surface up automatically because they inspect the live client.
