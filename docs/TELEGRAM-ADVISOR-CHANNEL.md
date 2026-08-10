# Telegram Advisor Channel

A **transport** for the existing Student Advisor. Not a second adviser, not a
second prompt, not a second academic policy. A linked student asks a question in a
private Telegram chat; the question reaches the same application service the web
chat calls, under the same self-only student principal, with the same tools, the
same privacy projection, the same rate limits and the same stored conversation.

Feature-flagged, **off by default** (`TELEGRAM_ADVISOR_ENABLED=false`).

---

## 1. Architecture

```text
Telegram private chat
  → POST /telegram/webhook/                    (HTTPS, X-Telegram-Bot-Api-Secret-Token,
                                                constant-time compare, fail closed)
  → parse_update()                             (private chats only, text only,
                                                chat.id == from.id, `message` only)
  → claim_update(update_id)                    (idempotency receipt — PK insert)
  → TelegramLink                               (verified telegram_user_id → student_id)
  → AdvisorPrincipal(role=STUDENT, student_id) (self-only, built from the link row)
  → core.services.advisor_turn.run_advisor_turn()   ←── SHARED WITH THE WEB
      ├─ ownership → validation → idempotency/replay → GENERATION budget
      ├─ persist student turn (AdvisorMessage, PENDING)
      ├─ answer_student_advisor(question, principal, history)   ← existing seam
      └─ persist_answer() → AdvisorMessage + AdvisorMessageCitation
  → formatting.render_answer()                 (plain text, no parse_mode, safe split)
  → transport.send_text()                      (Telegram Bot API)
```

### The seam

`core/services/advisor_turn.py` was **extracted from** `conversation_post_message_view`
(`core/advisor_conversation_views.py`). The HTTP view is now a thin adapter that
maps a `TurnResult` onto the same status codes and JSON it always produced; the
Telegram handler maps the same `TurnResult` onto sentences. There is exactly one
implementation of the turn.

That matters because every step of a turn is a security property held in place by
its **order**:

| Order | Step | Why here |
|---|---|---|
| 1 | ownership | a row belonging to another student never becomes a local variable |
| 2 | validation | an oversized question is refused before it costs anything |
| 3 | idempotency / replay | a retry serves the stored answer |
| 4 | **charge `GENERATION`** | after replay (so a replay is free), before the model (admission control) |
| 5 | persist the question | so a dropped response is resumable, not re-asked |
| 6 | call the adviser | outside the transaction — it can take ~90 s |
| 7 | persist the answer + citations | inside one transaction — all or nothing |

A second copy of that sequence would get the order subtly wrong and still work.

### What the channel does **not** contain

No prompt, no tool schema, no model client, no capability list, no policy
retrieval, no academic logic. `tests/test_telegram_gateway.py` asserts this
structurally: the gateway source may not name `answer_virtual_advisor`, any tool
in `STUDENT_V2_TOOL_NAMES`, or anything in `FORBIDDEN_STUDENT_V2_TOOLS`.

---

## 2. Authentication and linking

**A Telegram user id is not authentication.** It is an identifier anybody can
present. The identity comes from the browser session and nothing else.

**And a link token is not authentication either.** That is the correction a
security review forced, and it is the most important thing on this page. A token
is a *bearer* credential: whoever opens the URL reaches the page. The first design
bound `token.telegram_user_id` to `session.student_id` — two facts the server had
every right to trust individually, joined by nothing — and that is an account
takeover:

> An attacker types `/link` in their **own** chat, forwards the URL to a student
> ("activate the adviser bot"), and the student signs in through the university's
> real login on the university's real domain and presses one confirm button. The
> attacker's chat is now bound to the student's record: GPA, remaining courses,
> timetable, and the ability to open an escalation case in their name. The
> confirmation page cannot warn them — it stores no Telegram profile data, so it
> has no chat to name.

So the ceremony is **two-sided**, and the two secrets travel in opposite
directions:

```text
1. Student sends /link in a private chat.
2. Server mints 32 random bytes, stores ONLY sha256(token), sends the raw token
   once as a URL:  {TELEGRAM_PUBLIC_BASE_URL}/telegram/link/<token>/
                                                  ── server → chat → browser ──
3. Student opens it. Not signed in → redirect into the EXISTING student login
   flow (/student/login/, Uni ID → email OTP) with a validated ?next= back here.
4. Signed in → a confirmation page showing the full privacy notice and one button.
   There is NO student field on that form.
5. POST → approve_link():  APPROVES, BINDS NOTHING.
     a. AdvisorPrincipal.for_student(request)  ← student id, from the session only
     b. record approved_student_id + approved_at on the token
     c. mint a 6-character code, store ONLY sha256(code), show it in the BROWSER
                                                  ── server → browser → chat ──
6. Student returns to the chat and sends  /confirm <code>.
7. confirm_link() looks the code up among approvals **for THIS chat only**,
   compares it in constant time, claims the token with ONE conditional UPDATE,
   and creates TelegramLink under two partial unique constraints.
```

Re-run the attack against this: the victim approves, and the code appears in the
**victim's** browser. The attacker cannot type it. If the victim does the natural
thing — open the bot and try to confirm — the lookup is scoped to *their* chat,
finds no approval, and **fails safely**. Completing the attack now requires the
student to relay a secret that the page tells them never to share, through a
channel outside the flow.

| Requirement | How |
|---|---|
| Opaque token | `secrets.token_urlsafe(32)` — 43 chars, no encoded identifiers |
| Hashed at rest | `sha256` for **both** the token and the confirmation code |
| Short-lived | `TELEGRAM_LINK_TOKEN_TTL_SECONDS`, default 900 s, floor 60 s — binds the whole ceremony, not just its first half |
| Single use | one conditional `UPDATE`, not read-then-write |
| Channel binding | the code is redeemable **only** from the chat the token was minted in |
| Guessing bounded | `MAX_CONFIRM_ATTEMPTS = 5`, then the approval is burned |
| Student id never from URL/message/form | `AdvisorPrincipal.for_student(request)` |
| One chat ↔ one student | two **partial unique indexes** on `status='ACTIVE'` |
| No student id in messages or URLs | asserted by test |
| `/unlink` immediate | `TelegramLink.revoke()`, **re-checked again just before delivery** so a revocation landing mid-generation still stops the answer |
| Admin revocation | Django admin action, plus `/telegram/link/manage/` for the student |

> Why the code goes browser → chat and not chat → browser: both directions require
> a relay to break, but only this one **fails safely** for a victim who follows a
> forwarded link and then behaves naturally. The reverse ("here is a link and a
> code, enter the code on the page") is an ordinary-looking phishing message.

### The RBAC hazard this closes

`core/services/rbac.py::get_user_role` returns `ROLE_ADVISOR` for any authenticated
account with no matching group. `approve_link` therefore asserts **STUDENT
explicitly** via `AdvisorPrincipal.for_student`, which requires both the STUDENT
role and a `UserScope.student_id`. A staff account confirming a link gets `403`.
Tested: `test_a_non_student_account_cannot_complete_a_link`.

### Redirect-after-login

The student login views generated `?next=` (via `login_required`) and **discarded
it**, so there was no way back from sign-in to a linking page. Added:
`_remember_next` / `_post_login_redirect` in `core/student_auth_views.py`.

The value is held in the **session**, not round-tripped through the two login
forms, and is accepted only if it is a same-host relative path
(`url_has_allowed_host_and_scheme`, plus an explicit `//` rejection for
protocol-relative URLs). Staff sessions never follow a student's `next`. Five
open-redirect payloads are tested.

---

## 3. Security properties

| # | Property | Where |
|---|---|---|
| 1 | Official Bot API only | `telegram_gateway/transport.py` |
| 2 | HTTPS | deployment (Render terminates TLS); `TELEGRAM_PUBLIC_BASE_URL` must be `https://` |
| 3 | `secret_token` validated, `hmac.compare_digest` **on bytes** | `views._secret_ok` — compared as `str` it raises `TypeError` on any non-ASCII header, i.e. an unauthenticated 500 on the one view whose job is refusing them |
| 4 | Unsupported methods → 405 | `@require_POST` |
| 5 | Private chats only | `bot.parse_update` |
| 6 | Group/supergroup/channel/inline refused **silently** | `bot.parse_update` — replying into a group would itself be the disclosure |
| 7 | Only `message` updates | `SUPPORTED_UPDATE_KEYS` |
| 8 | `update_id` idempotency | `TelegramUpdateReceipt` (PK) **and** `AdvisorMessage.idempotency_key = "tg:<update_id>"`. The receipt is claimed before the work and **released if the work raises**, so a crash does not make a question permanently unaskable |
| 9 | No raw payload stored | receipt has exactly `{update_id, received_at}` — asserted by test |
| 10 | No content/token/id logging | asserted by `test_no_secret_or_identifier_reaches_the_logs` |
| 11 | Redacted operational logs | log lines carry no interpolated identifiers at all |
| 12 | Credentials only in env | asserted by `test_credentials_live_only_in_the_environment` |
| 13 | Fail closed | unset secret → 403 always; unset bot token → no socket; unset base URL → `/link` refuses |
| 14 | Feature flag, default off | `TELEGRAM_ADVISOR_ENABLED` |
| 15 | No files/images/contacts/locations/voice | non-text messages get one refusal, nothing is fetched |
| 16 | No live bot created, no credentials committed | this change configures nothing |

**Deliberately not copied from `whatsapp_gateway`:** its signature check returns
`not require_signature` when unconfigured, and its default is
`"false" if DEBUG else "true"`. Open-in-development is a default that travels.
Here an unset secret is a refusal, unconditionally.

The webhook is the repo's second `csrf_exempt` view. It reads **no** session and
**no** cookie — its only authority is the header. The linking pages are a separate
door: session-authenticated and CSRF-protected.

---

## 4. Privacy

**What is processed:** the text of the question, and the Telegram user id — for
one purpose, replying under the student's verified university identity.

**What the university stores:** the `telegram_user_id → student_id` mapping, and
the questions and answers in the *existing* `AdvisorConversation` /
`AdvisorMessage` tables the student already sees on the web.

**What is deliberately not stored:** Telegram display name, username, phone
number, profile photo, and any raw update payload. None is needed to deliver an
answer. `test_no_telegram_profile_information_is_stored` fails if a column with
any of those names is added.

**Telegram is an external cloud service and bot chats are not end-to-end
encrypted.** `/privacy` says so in Arabic and English, and the same text is shown
on the confirmation page *before* the button — not linked from it.

**Retention.** `/unlink` revokes the mapping immediately. It does **not** delete
the conversation: that history lives in the student's university account under the
platform's existing retention policy. The `/privacy` text states this plainly
rather than implying unlinking erases anything.

**Model training.** Conversation data is not used to train any model; the remote
provider boundary is unchanged (`llm_remote_privacy.project_tool_result_for_remote`
still decides what leaves the building, and the channel does not touch it).

**Only `AdvisorMessage.content` is ever sent.** The adviser's result dict also
carries the agent trace — and, on the V1 branch that `STUDENT_ADVISOR_V2_ENABLED`
still defaults to, a `verified_context` key holding the student's unprojected
record. `bot._render_outcome` reads the stored message and never the result dict.

### Open product-owner decision — marks, GPA and failed-course grades

**Not resolved by this change, and it gates production enablement.**

The adviser answers questions about a student's own record, and an answer may
legitimately contain a GPA or a grade. Suppressing those *for this channel only*
is a new **channel axis** through `ToolBoundary.project_tool_result` /
`project_context` — post-filtering the Arabic prose afterwards is the wrong fix
and would mangle correct answers.

No such axis was built. The mitigation is that the channel is **off by default**,
so nothing is disclosed until a product owner turns it on. Decide before enabling:

- may a Telegram answer state a GPA?
- may it state an individual course mark or a failed-course grade?

If the answer to either is no, that work is a prerequisite, not a follow-up.

---

## 5. Configuration

All via environment variables. Nothing is committed.

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_ADVISOR_ENABLED` | `false` | Off means `/telegram/webhook/` answers 404 |
| `TELEGRAM_BOT_TOKEN` | `""` | From BotFather. Empty ⇒ no socket is ever opened |
| `TELEGRAM_WEBHOOK_SECRET` | `""` | Empty ⇒ **every** webhook call is refused |
| `TELEGRAM_BOT_USERNAME` | `""` | Informational |
| `TELEGRAM_PUBLIC_BASE_URL` | `""` | e.g. `https://advisor-system-v9zs.onrender.com`. Empty ⇒ `/link` refuses |
| `TELEGRAM_LINK_TOKEN_TTL_SECONDS` | `900` | Floor 60 s |
| `TELEGRAM_API_TIMEOUT_SECONDS` | `30` | |
| `TELEGRAM_PRIVACY_URL` | `""` | Optional external notice; the built-in one is always shown |
| `TELEGRAM_DISPATCH_SYNC` | `false` | Debugging only. Always on under pytest |

### Local setup

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put in `.env`:

```
TELEGRAM_ADVISOR_ENABLED=true
TELEGRAM_BOT_TOKEN=<from BotFather>
TELEGRAM_WEBHOOK_SECRET=<the value generated above>
TELEGRAM_BOT_USERNAME=<yourbot>
TELEGRAM_PUBLIC_BASE_URL=https://<your-https-tunnel-or-host>
```

Then:

```bash
.venv/Scripts/python.exe manage.py migrate telegram_gateway
```

Telegram requires a public HTTPS endpoint. `localhost` will not work — use a
tunnel for development.

### BotFather

1. Open `@BotFather` in Telegram → `/newbot` → name → username ending in `bot`.
2. Copy the token into `TELEGRAM_BOT_TOKEN`. **Never commit it.** If it leaks,
   `/revoke` in BotFather.
3. `/setprivacy` → **Enable**. Privacy mode keeps the bot from receiving ordinary
   group messages — defence in depth beside the code's own private-chat filter.
4. `/setjoingroups` → **Disable**. The bot has no group function at all.
5. `/setcommands`:

```
start - بدء المحادثة · Start
link - ربط الحساب الجامعي · Link your university account
help - المساعدة · Help
privacy - الخصوصية · Privacy
new - محادثة جديدة · New conversation
advisor - تحويل إلى مرشد بشري · Escalate to a human adviser
unlink - إلغاء الربط · Unlink
```

### setWebhook

Registers the endpoint, the secret, and the **only** update type the server
accepts:

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://<host>/telegram/webhook/","secret_token":"<TELEGRAM_WEBHOOK_SECRET>","allowed_updates":["message"],"drop_pending_updates":true,"max_connections":20}'
```

Verify (the response must show your URL, `pending_update_count: 0`, and no
`last_error_message`):

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo"
```

`secret_token` is what Telegram echoes in `X-Telegram-Bot-Api-Secret-Token`. It
must equal `TELEGRAM_WEBHOOK_SECRET` exactly or every update is refused with 403.

---

## 6. Migrations

One migration, `telegram_gateway/0001_initial.py`, dependent on
`core.0061_scope_student_term_section_uniqueness`. It touches **no existing
table**. Three new tables:

- `telegram_links` — two partial unique indexes (`uq_tg_active_telegram_user`,
  `uq_tg_active_student`), both `WHERE status='ACTIVE'`
- `telegram_link_tokens`
- `telegram_update_receipts`

Deterministic (`makemigrations --check` reports no changes) and reversible
(`CreateModel` only; asserted by `test_the_migration_is_reversible`).

```bash
.venv/Scripts/python.exe manage.py migrate telegram_gateway        # apply
.venv/Scripts/python.exe manage.py migrate telegram_gateway zero   # roll back
```

> Partial unique indexes are enforced by both SQLite and PostgreSQL 16. Index
> names are global in PostgreSQL — these are prefixed `uq_tg_` and do not collide.

---

## 7. Reliability

The webhook **acknowledges and hands off**. An adviser turn is budgeted at up to
4 tool iterations × 75 s; answering inline would exceed Telegram's patience,
Telegram would redeliver, and the redelivery would run the model again.

`telegram_gateway/runner.py` reuses the shim
`core/services/planner_job_runner.py` already uses — a process-local
`ThreadPoolExecutor` plus `close_old_connections` on both sides.

**No Redis, no Celery, no broker was added.** Justification: the project has none,
adding one for a single background call makes the deployment depend on a service
nothing else needs, and Render would need a second process type. The durable
alternative already in the repo (`core/services/timetable_repair_jobs.py`, drained
by the `repair_worker` management command) is the correct escalation if delivery
must survive a restart.

**Limits of this choice:** process-local and not durable. A worker killed mid-turn
leaves the question `PENDING` and the answer undelivered to the chat. It is not
lost — the row is in the same table the web adviser reads, so the student sees it
on the web, and `advisor_turn.is_resumable` treats a turn stranded past
`STALE_GENERATION` (15 min) as answerable again, so re-asking works instead of
being refused as a duplicate.

Telegram API timeouts and delivery failures are **absorbed**, never raised:
`transport.send_text` returns a failure dict. A raise would make the webhook
non-200 → redelivery → a second model call for an answer already generated.

**Sends made inside the request use a 3-second deadline**
(`transport.INLINE_TIMEOUT_SECONDS`), not the 30-second one. Render runs gunicorn
with two *sync* workers for the entire platform, so a Telegram stall on the
request path is not a slow reply — it is the site having no worker left. The
background path keeps the full `TELEGRAM_API_TIMEOUT_SECONDS`, where blocking
costs nothing but the answer.

---

## 8. Production deployment checklist

- [ ] Product owner has ruled on GPA / marks / failed-course grades (§4)
- [ ] `TELEGRAM_WEBHOOK_SECRET` generated with a CSPRNG, ≥ 32 chars, set in Render
      env (`sync: false`), **not** in the repo
- [ ] `TELEGRAM_BOT_TOKEN` set in Render env, never committed
- [ ] `TELEGRAM_PUBLIC_BASE_URL` is the real **https://** origin
- [ ] `manage.py migrate telegram_gateway` applied
- [ ] `setWebhook` called with `secret_token` and `allowed_updates: ["message"]`
- [ ] `getWebhookInfo` shows no `last_error_message`
- [ ] BotFather privacy mode **enabled**, join-groups **disabled**
- [ ] `TELEGRAM_ADVISOR_ENABLED=true` set **last**
- [ ] Smoke test with a test bot and a test student (§10)
- [ ] Schedule `linking.purge_expired()` (spent tokens + old receipts)

## 9. Rollback / disable

**Fastest (seconds), no deploy:** set `TELEGRAM_ADVISOR_ENABLED=false` and
restart. The webhook answers 404 and nothing academic is reachable.

**Stop Telegram calling at all:**

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/deleteWebhook?drop_pending_updates=true"
```

**Kill the credential:** BotFather → `/revoke`. Every call with the old token dies.

**Revoke one student's access:** Django admin → Telegram links → select → *Revoke
the selected Telegram links*. Or `linking.revoke_links_for_student(student_id)`.

**Full removal:** `migrate telegram_gateway zero`, drop `telegram_gateway` from
`INSTALLED_APPS` and `config/urls.py`. Nothing in `core` depends on it.
`core/services/advisor_turn.py` **stays** — the web adviser uses it.

---

## 10. Manual test script (test bot + test student)

Use a **separate** BotFather bot and a test student. Never the production bot.

| # | Action | Expected |
|---|---|---|
| 1 | `/start` in a private chat | Welcome + how to link. No student data |
| 2 | `كم معدلي؟` before linking | Linking instructions only. No record, no student number |
| 3 | `/privacy` | Notice incl. "not end-to-end encrypted" and how to unlink |
| 4 | `/link` | A single-use URL with a stated expiry, no identifiers in it |
| 5 | Open it signed out | Lands in the **existing** student login (Uni ID → OTP) |
| 6 | Complete login | Returns to the confirmation page, privacy text visible |
| 7 | Approve | Web shows a `/confirm <code>`; **nothing is linked yet**; the chat gets NO code |
| 7b | Send `/confirm <code>` in the chat | Bot confirms the link |
| 7c | **Forwarded-link drill**: mint a token in bot chat A, open the URL in a browser signed in as the test student, approve, then try `/confirm <code>` from a *different* chat B | Refused. Nothing linked |
| 8 | Reopen the same URL | "expired or already used" |
| 9 | Ask `ما المواد المتبقية لي؟` | Brief ack, then a real Arabic answer about **this** student |
| 10 | Compare with the web adviser | Same student, same conversation thread |
| 11 | Follow-up (`وماذا عن الفصل القادم؟`) | Understands the reference |
| 12 | Ask something that produces a long answer | Split into several messages; sources whole and last |
| 13 | `سجّل لي مادة AI351` | Refused. No registration is performed |
| 14 | `/new`, then a follow-up | No memory of the previous thread |
| 15 | `/advisor` after an answer | Case reference returned; visible in the adviser inbox |
| 16 | Add the bot to a group, ask there | **No reply at all** |
| 17 | Send a photo / voice note | One short refusal. Nothing downloaded |
| 18 | `/unlink`, then ask again | Linking instructions. No data |
| 19 | Replay a webhook body with the same `update_id` | `{"duplicate": true}`; no second answer, no second model call |
| 20 | `curl -X POST .../telegram/webhook/` with no secret header | `403` |
| 21 | `curl -X GET .../telegram/webhook/` | `405` |
| 22 | `grep` the server log for the question text, the token, the confirmation code, the student id | Nothing |
| 23 | `curl` the webhook with a non-ASCII secret header (`-H $'X-Telegram-Bot-Api-Secret-Token: Ã©'`) | `403`, not `500` |
| 24 | Ask a question, then `/unlink` from another device while it is still generating | The answer never arrives in the chat; it is on the web |

---

## 11. Known limitations

1. **GPA / marks / failed grades are not channel-suppressed.** Open product-owner
   decision (§4). Mitigated only by the default-off flag.
2. **Background execution is not durable.** A restart mid-turn strands the turn
   `PENDING` and the chat gets no answer; the student sees it on the web and can
   re-ask. Durable delivery = migrate to `timetable_repair_jobs`.
3. **Plain text only.** No bold, no tables, no inline keyboards, no buttons. The
   deliberate cost of having no escaping bug.
4. **Structured cards are a link, not a render.** Timetable and graduation
   presentations point at the web screen (§ requirement: do not recreate the UI).
5. **Arabic bidi is unaddressed here.** The web fix for reversed time ranges
   («09:00-10:15» painted backwards) lives in the template layer. Telegram renders
   client-side and applies its own bidi; a Telegram-specific isolation pass was
   **not** written and time ranges may render reversed on some clients. Verify at
   step 9/12 of the manual script.
6. **One conversation per chat.** No thread switching from Telegram; `/new` is the
   only control. The web sidebar remains the full interface.
7. **`/advisor` escalates the most recent answered turn only.**
8. **No outbound retry.** A failed send is not retried; the answer is stored and
   visible on the web.
9. **No delivery of adviser replies to escalations.** A resolved case is not
   pushed to Telegram; the student checks the platform.
10. **Receipts and tokens need periodic purging** (`linking.purge_expired`); no
    scheduler entry is created by this change.
11. **The `csrf_exempt` webhook is the repo's second.** It reads no session and no
    cookie, but it is a permanent exception worth re-reviewing if it ever grows.

---

## 12. Files

**New**

```
core/services/advisor_turn.py          the channel-neutral turn (extracted)
telegram_gateway/                      models, linking, bot, transport,
                                       formatting, messages, runner, views,
                                       urls, admin, migrations/0001_initial.py
telegram_gateway/templates/…           link_confirm, link_result, link_manage
tests/test_telegram_gateway.py         134 tests
docs/TELEGRAM-ADVISOR-CHANNEL.md       this file
```

### The review that changed the design

A four-lens adversarial review (auth, webhook/transport, extraction fidelity,
mutation testing) ran against the first implementation. It found one **blocker** —
the link ceremony had no channel binding (§2) — plus an unauthenticated 500 on a
non-ASCII secret header, a revocation that did not stop an in-flight answer, a
30-second blocking send on a 2-worker deployment, a receipt that burned an
`update_id` on crash, and a stale `?next=` inheritable across sign-ins on a shared
machine. All are fixed above. The extraction lens confirmed **no web behaviour
changed**.

Every fix carries a test that was verified by mutation: the fix is broken, the
suite is run, and the suite goes red. 16/16.

**Modified**

```
core/advisor_conversation_views.py     now a thin adapter over advisor_turn
core/student_auth_views.py             validated redirect-after-login
config/settings.py                     INSTALLED_APPS + TELEGRAM_* block
config/urls.py                         path("telegram/", …)
tests/test_advisor_conversations.py    two white-box helpers moved modules
tests/test_advisor_escalation.py       one patch target moved modules
```
