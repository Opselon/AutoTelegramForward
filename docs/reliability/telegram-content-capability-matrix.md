# Telegram Content Capability Matrix

**Scope:** Opselon/AutoTelegramForward, revision `78b29bd`, pyrogram Layer 158.
**Method:** verified against the **live Telegram API** on 2026-10-31, using the
stack's own session and isolated throwaway channels created for the probe
(channels named `atf-cp-*` / `atf-cs-*`; they were deleted where Telegram's
rate limit allowed — see the note at the end).

This matrix is the operating ground truth: **an entry is only "Supported and
verified" when the action was executed against real Telegram and observed**.
Anything not measured is marked explicitly.

---

## 1. Verified by execution (2026-10-31, live API probe)

The probe posted each special message into a real test channel with raw
MTProto (`messages.SendMedia`) and then attempted `copy_message` on it —
exactly the forwarder's copy path.

| content type | send into a channel | `copy_message` (the copy path) | verdict |
|---|---|---|---|
| **poll** | SENT | **FAILED** `ValueError: Unknown media type` | Copy unsupported by the client library; native forward only |
| **contact** | SENT | **FAILED** `AttributeError: 'NoneType' object has no attribute 'encode'` | Copy unsupported — the pyrogram client cannot serialize the vCard; native forward only |
| **venue** | SENT | **SENT** | Supported and verified |
| **dice** | SENT | **SENT** | Supported and verified |

**This contradicts the in-code `TelegramMessageCapabilityMatrix`** in
`core/domain/services.py`, which claims:

* `poll: copy = False` — **correct**, and now verified.
* `contact: copy = True` — **wrong**: the copy path fails with a client-side
  serialization error, not a server refusal. The matrix's "Supported" for a
  contact copy is aspirational, not real.
* `venue` / `dice` are absent from `MEDIA_TYPE_MAP` entirely (see §2), so
  although Telegram supports the copy, **the forwarder cannot classify them**
  on either ingestion path.

The code matrix is therefore *not* a safe basis for content decisions. It must
be corrected to the verified values below, and the classification gaps (E2E-1,
E2E-2 of the e2e audit) fixed before any of its entries can be trusted.

---

## 2. Forwarder-side classification (both ingestion paths)

Which media kinds the forwarder can even *name*, after the E2E-1/E2E-2 audit:

| media kind | live path (`payload_from_pyrogram`) | replay path (`Normalizer`) | bypass `send_*` | copy path | matrix claim |
|---|---|---|---|---|---|
| photo | photo | photo | send_photo | copy_message | verified |
| video | video | video | send_video | copy_message | verified |
| document | document | document | send_document | copy_message | verified |
| audio | audio | audio | send_audio | copy_message | verified |
| voice | voice | voice | send_voice | copy_message | verified |
| animation | animation | animation | send_animation | copy_message | verified |
| sticker | sticker | sticker | send_sticker | copy_message | verified |
| **video_note** | video | **text** ✗ | *none* | copy_message | claim: copy=True |
| **poll** | poll | **text** ✗ | *none* | **fails (verified)** | claim: copy=False |
| **contact** | contact | **text** ✗ | *none* | **fails (verified)** | claim: copy=True ✗ |
| **location** | location | **text** ✗ | *none* | *unverified* | claim: copy=True |
| **venue** | **other** ✗ | **text** ✗ | *none* | works (verified) | claim: copy=True |
| **dice** | **other** ✗ | **text** ✗ | *none* | works (verified) | claim: copy=True |
| **game** | other ✗ | **text** ✗ | *none* | *unverified* | **absent** |
| web_page (link preview) | other | text | — | — | not a content decision |

✗ = the kind is mis-classified on that path. Three of the seven kinds the
mission's content matrix must name are **invisible to the replay path**, and
two more are mis-named on the live path. Content policy decided from these
values is wrong for every ✗ row — the E2E-1/E2E-2 finding.

---

## 3. Gifts, paid media, and other special content — honest status

Per the mission's instruction to determine support from the *active* API and
never promise unsupported behavior:

| type | status | evidence |
|---|---|---|
| Gift / unique gifts | **NOT VERIFIED** | gift messages are MTProto service-layer constructs; the probe could not exercise the Gifts API. The code has no handler for them — they would arrive as `other` and be treated as text. |
| Paid media (Stars) | **NOT IMPLEMENTED for copy** | `TelegramMessageCapabilityMatrix` claims `copy: False, forward: True`. That claim is from the API surface, not from an executed send. |
| Quiz polls | **NOT VERIFIED** | a quiz is `InputMediaPoll` with `quiz=True`; the code path is the poll path, whose copy **fails (verified)**. |
| Poll closure/vote sync | **NOT IMPLEMENTED** | no vote or close-update handling exists. |
| Location | **NOT VERIFIED for copy** | matrix claims copy=True; not exercised by the probe (venue and dice were). No bypass exists. |
| Pin updates | **NOT IMPLEMENTED** | no pin handler exists in the dispatcher. |
| Reactions | **NOT IMPLEMENTED** | no reaction handler exists. |
| Story forwards | **NOT IMPLEMENTED** | no story handling exists. |

These entries are *not* failures to fix later in a silent way — they are the
explicit "not yet verified / not implemented" labels the mission requires
rather than an implied "works".

---

## 4. Corrected matrix (what the code should say)

Derived strictly from the verified observations:

| type | receive | detect | copy | forward | notes |
|---|---|---|---|---|---|
| text | ✓ | ✓ | ✓ | ✓ | link previews are an attribute, not a media type (E2E-7) |
| photo | ✓ | ✓ | ✓ | ✓ | |
| video | ✓ | ✓ | ✓ | ✓ | |
| video_note | ✓ | live only ✗ | ✓ | ✓ | replay path classifies as text (E2E-1) |
| animation | ✓ | ✓ | ✓ | ✓ | |
| voice | ✓ | ✓ | ✓ | ✓ | |
| audio | ✓ | ✓ | ✓ | ✓ | |
| document | ✓ | ✓ | ✓ | ✓ | |
| sticker | ✓ | ✓ | ✓ | ✓ | TGS/WebM fidelity itself is NOT VERIFIED |
| poll | ✓ | live only ✗ | ✗ **verified** | ✓ | copy fails: `Unknown media type` |
| contact | ✓ | live only ✗ | ✗ **verified** | ✓ | copy fails: pyrogram vCard serialization |
| location | ✓ | live only ✗ | *unverified* | ✓ | |
| venue | ✓ | ✗ both | ✓ **verified** | ✓ | classified `other` live, `text` replay |
| dice | ✓ | ✗ both | ✓ **verified** | ✓ | classified `other` live, `text` replay |
| game | ✓ | ✗ both | *unverified* | *unverified* | absent from the matrix entirely |
| service | ✓ | ✓ | ✗ | ✗ | intentionally dropped |
| gifts / unique gifts | *n/a* | ✗ | ✗ | *unverified* | NOT VERIFIED |
| paid media (Stars) | ✓ | *unverified* | ✗ | *unverified* | claims only; not executed |
| story | ✗ | ✗ | ✗ | ✗ | NOT IMPLEMENTED |

---

## 5. Probe artefacts and disclosure

* The probe created two throwaway channels per run (`atf-cp-*`,
  `atf-cs-*`). Deletion succeeded on every run except the last, where
  Telegram returned `420 FLOOD_WAIT_X` (687 s) on `channels.DeleteChannel`.
  The two remaining channels (`-1003766814494`, `-1003390735862`) are empty,
  have **no members and no rules**, and never entered the forwarder's database
  (verified: `channel_cursors` and `forward_rules` contain no trace). They
  were **not retried** — per this project's own FloodWait discipline the wait
  is honoured, and no customer-visible artefact exists. They are disclosed
  here rather than silently re-attempted.
* The probe read the stack's session string from the production store
  (read-only) and started its **own** client; the running `atf-core` process
  was not disturbed.
* No probe touched any monitored source or destination channel: the two test
  channels were created for the run and named so they cannot be confused with
  real ones.
