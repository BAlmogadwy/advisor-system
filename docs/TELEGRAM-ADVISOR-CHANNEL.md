# Telegram Advisor Channel

A **transport** for the existing Student Advisor. Not a second adviser and not a
second academic policy. A linked student asks a question in a private Telegram
chat; the question reaches the same application service the web chat calls under
the same self-only student principal, conversation store and adviser-generation
budget. Channel-specific ingress and command budgets are additional controls.

The evidence surface is intentionally **not** the same. Telegram always uses
Student Advisor V2 under the server-selected `telegram_safe` execution profile,
even while the web V2 feature flag is off. That profile removes transcript-shaped
capabilities, projects every tool/fallback result, and admits as generation
history only prior Telegram turns promoted to `telegram_safe`. The authenticated
web adviser remains the full-record surface.

Feature-flagged, **off by default** (`TELEGRAM_ADVISOR_ENABLED=false`).

---

## 1. Architecture

```text
Telegram private chat
  → POST /telegram/webhook/                    (HTTPS, X-Telegram-Bot-Api-Secret-Token,
                                                constant-time compare, fail closed)
  → parse_update()                             (private chats only, text only,
                                                chat.id == from.id, `message` only)
  → enqueue update_id + normalized work        (one PostgreSQL transaction; durable
                                                idempotency receipt / queue row)
                                                question starts briefly unavailable
  → send “Received — preparing…”               (questions only; after commit)
  → release job in finally                     (or bounded timestamp self-releases it)
  ← 200                                        (after durable enqueue and ack attempt)

Persistent telegram_advisor_worker
  → lease ready work                           (per-link FIFO; stale leases recover)
  → revalidate TelegramLink + student access   (fail closed at execution time)
  → AdvisorPrincipal(role=STUDENT, student_id) (self-only, rebuilt from current data)
  → personal-record intent gate                (known exact-result requests stay web-only)
  → core.services.advisor_turn.run_advisor_turn()   ←── SHARED WITH THE WEB
      ├─ ownership → validation → idempotency/replay → GENERATION budget
      ├─ persist student turn (AdvisorMessage, PENDING, telegram_unvalidated)
      ├─ load only server-profiled Telegram-safe history
      ├─ answer_student_advisor(...) → V2 forced for telegram_safe
      │    └─ restricted schemas + projected evidence + safe fallback
      └─ persist_answer() → AdvisorMessage + AdvisorMessageCitation
  → personal-record output DLP                 (prose + visible card fields)
  → formatting.render_answer()                 (plain text, no parse_mode, safe split)
  → persist typed text/photo delivery manifest (recipes only; no PNG or remote metadata)
  → render photos in bounded secret-free child (optional phase, independent retry budget)
  → transport.send_photo(); advance JSON photo cursor
  → transport.send_text(); advance DB text cursor
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

`answer_student_advisor` remains the shared model seam, but
`channel_profile="telegram_safe"` is a hard runtime selection: it routes to V2
even when `STUDENT_ADVISOR_V2_ENABLED=false`. The legacy adviser has no
channel-specific evidence projection, so falling back to it would silently reopen
the full-record surface. Section 4 describes the layers around this shared turn.

### What the channel does **not** contain

The `telegram_gateway` package contains no standalone adviser prompt, tool schema,
model client, policy retrieval or academic decision logic. It selects the
server-owned safe profile and applies transport-side input/output privacy checks;
the profile's system rules, schema reduction and evidence projection live beside
the shared V2 runtime in `core.services`. `tests/test_telegram_gateway.py` asserts
structurally that `bot.py` names no capability in `STUDENT_V2_TOOL_NAMES` or
`FORBIDDEN_STUDENT_V2_TOOLS`.

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
   During the temporary production acceptance test only,
   `TELEGRAM_LINK_OTP_REDIRECT_EMAIL` may route that OTP to the test operator.
   The server re-resolves the saved destination and rechecks that its invitation
   is fresh and live before applying the override; ordinary student login,
   invalid/expired invitations, and stale login attempts still use the student's
   university mailbox. `STUDENT_OTP_REDIRECT_EMAIL` remains the separate global
   testing override and takes precedence when configured. Clear the link-only
   setting immediately after acceptance testing.
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
| 8 | `update_id` idempotency | `TelegramUpdateReceipt` is both the PK receipt and durable job envelope; a duplicate cannot enqueue a second turn, and a process crash leaves claimable work |
| 9 | No raw update stored | queue rows keep only normalized work/delivery fields needed for execution; terminal cleanup removes old rows |
| 10 | No content/token/id logging | asserted by `test_no_secret_or_identifier_reaches_the_logs` |
| 11 | Redacted operational logs | log lines carry no interpolated identifiers at all |
| 12 | Credentials only in env | asserted by `test_credentials_live_only_in_the_environment` |
| 13 | Fail closed | unset secret → 403 always; unset bot token → no socket; unset base URL → `/link` refuses |
| 14 | Feature flag, default off | `TELEGRAM_ADVISOR_ENABLED` |
| 15 | No files/images/contacts/locations/voice | non-text messages get one refusal, nothing is fetched |
| 16 | No live bot created, no credentials committed | this change configures nothing |
| 17 | Reduced evidence surface | `TELEGRAM_SAFE_PROFILE` forces V2, removes transcript-shaped schemas, and projects results before model or fallback use |
| 18 | History provenance is server-owned | `AdvisorMessage.generation_profile`; a client-supplied idempotency prefix cannot opt a web answer into Telegram history |
| 19 | Generated output is quarantined until validated | the turn starts `telegram_unvalidated`; only output DLP can promote it to `telegram_safe`, otherwise only the authenticated-web notice reaches Telegram |

**Deliberately not copied from `whatsapp_gateway`:** its signature check returns
`not require_signature` when unconfigured, and its default is
`"false" if DEBUG else "true"`. Open-in-development is a default that travels.
Here an unset secret is a refusal, unconditionally.

The webhook is the repo's second `csrf_exempt` view. It reads **no** session and
**no** cookie — its only authority is the header. The linking pages are a separate
door: session-authenticated and CSRF-protected.

---

## 4. Privacy

**What is processed:** the question text, the validated adviser answer, and the
Telegram user id — for one purpose, replying under the student's verified
university identity. When that answer contains a current, expected, or proposed
timetable presentation, the channel may also render and send images containing
the planning term; course names/codes and sections; class days and times; credit
load; requested must-take and pinned-section constraints; and unplaced courses or
reasons a requested constraint could not be satisfied.

**What the university stores:** the `telegram_user_id → student_id` mapping, and
the questions and answers in the *existing* `AdvisorConversation` /
`AdvisorMessage` tables the student already sees on the web. While work is queued,
the durable job envelope also holds the normalized question needed to execute it;
that input is cleared when delivery is materialized or the job becomes terminal.
For a timetable answer the queue stores only typed image recipes referencing the
existing adviser message, not PNG bytes or Telegram file metadata. The recipes are
cleared with the rest of the delivery payload when the job becomes terminal.

**What is deliberately not stored:** Telegram display name, username, phone
number, profile photo, and any raw update payload. None is needed to deliver an
answer. `test_no_telegram_profile_information_is_stored` fails if a column with
any of those names is added.

**Telegram is an external cloud service and bot chats are not end-to-end
encrypted.** Sent timetable and graduation-plan images are retained under
Telegram's policies and can
be downloaded or forwarded like other Telegram media. `/privacy` says so in
Arabic and English, and the same text is shown on the confirmation page *before*
the button — not linked from it.

**Retention.** `/unlink` revokes the mapping immediately. It does **not** delete
the conversation: that history lives in the student's university account under the
platform's existing retention policy. Nor can it retract messages or timetable
images already sent to Telegram. The `/privacy` text states both facts plainly
rather than implying unlinking erases anything. Old terminal queue metadata is
deleted by the daily retention job seven days after `finished_at`; legacy inline
receipts that predate that field fall back to `received_at`. `QUEUED` and
`RUNNING` work is excluded regardless of age.

**Model training.** Conversation data is not used to train any model. A remote
backend still passes through its existing provider boundary, and the Telegram
profile is an additional, narrower projection rather than a replacement for it.

### Layered exact-record boundary

Exact GPA/CGPA, marks, letter grades, failed-course results, transcript detail and
registrar academic standing stay in the authenticated web adviser. This is held by
several independent layers:

1. **Intent gate before generation.** `bot.requires_secure_record_surface`
   recognises explicit Arabic/English personal-record requests and returns the
   authenticated-web notice without calling the model or creating an adviser turn.
   General policy questions such as “How is GPA calculated?” remain answerable.
2. **Forced reduced V2 evidence.** `telegram_safe` forces Student Advisor V2 even
   if the web rollout flag is false. `get_student_context` and `my_plan_by_term`
   are not advertised; all remaining tool results are recursively projected to
   remove exact-result and status-derived fields before the model or deterministic
   answer logic sees them. Cached/repeated tool responses, forced-answer paths and
   deterministic renderers consume those projected copies too. If the tool loop
   needs seeded fallback evidence, it uses projected `my_progress`, not the full
   student-context fallback.
3. **Profiled history only.** `core.services.advisor_history.load_profiled_history`
   loads only questions whose server-owned `AdvisorMessage.generation_profile` is
   `telegram_safe` and the assistant messages directly paired with those
   questions. Web turns, legacy turns and withheld Telegram turns are excluded.
   Only the output boundary can promote a turn to that state; an idempotency key
   supplied by a web client cannot forge this provenance.
4. **Output data-loss prevention (DLP) before Telegram delivery.** The persisted
   assistant text is checked both for personal-result language and against the
   student's current structured GPA/course-result values. If it still discloses
   a protected result, the generated answer is withheld; otherwise an atomic
   conditional update promotes the question from `telegram_unvalidated` to
   `telegram_safe`. Telegram receives only the authenticated-web notice for a
   withheld answer.

The model result dictionary and raw tool evidence are never used as the outbound
message. A safe answer is rendered from the persisted `AdvisorMessage`; a withheld
answer remains visible only through the authorised web conversation. Its student
turn is durably changed from `telegram_unvalidated` to `telegram_withheld`, so the
decision survives retries and neither that question nor its assistant answer can
enter future Telegram generation history. A crash after answer persistence but
before output validation leaves the turn unvalidated; replay fails closed by
withholding and marking it, without testing the old answer against a student
record that may since have changed. Concurrent finalisers use a compare-and-swap
and respect the first durable decision.

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
| `TELEGRAM_LINK_AUTH_MAX_AGE_SECONDS` | `600` | Maximum age of the session-local student authentication proof used to approve a link |
| `TELEGRAM_MAX_PENDING_PER_LINK` | `10` | Hard cap on queued/running work per linked chat; `/unlink` is never throttled |
| `TELEGRAM_API_TIMEOUT_SECONDS` | `30` | |
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
.venv/Scripts/python.exe manage.py migrate
```

Keep the durable queue worker running in a second terminal:

```bash
.venv/Scripts/python.exe manage.py telegram_advisor_worker --sleep 1 --max-attempts 3
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

Use the management command so the bot token stays in the environment instead of
appearing in shell history, process listings, terminal scrollback, or pasted
support logs:

```bash
python manage.py telegram_webhook --set
```

The command registers `allowed_updates=["message"]`, the configured secret, and
`max_connections=1` so ingress matches the ordered durable queue. It drops stale
pending updates by default; use `--keep-pending` only for an intentional recovery.
Verify after registration (the response must show the expected URL,
`max_connections: 1`, and no `last_error_message`):

```bash
python manage.py telegram_webhook --info
```

`secret_token` is what Telegram echoes in `X-Telegram-Bot-Api-Secret-Token`. It
must equal `TELEGRAM_WEBHOOK_SECRET` exactly or every update is refused with 403.
The command validates the HTTPS origin and secret format and never prints the
credential.

---

## 6. Adviser card images

The durable worker sends timetable PNGs when
`TELEGRAM_SEND_TIMETABLE_IMAGES=true`. This includes a card for a current or
expected timetable when the adviser presents one, as well as generated timetable
alternatives. A card can show the planning term, course and section details,
meeting days and times, credit load, requested must-take/pinned constraints, and
unplaced courses or constraint-failure reasons. Images are delivery items in the
same database-backed queue as the answer text; they never bypass the worker after
the model turn.

Graduation-plan maps require the independent
`TELEGRAM_SEND_GRADUATION_IMAGES=true` switch. One image may show the planning
baseline, completed/baseline/projected/unresolved course states, prerequisite
links, requested scenario changes, and unresolved requirements. This flag stays
off unless that broader export has been explicitly accepted; enabling timetable
images alone does not enable it.

The materialized payload is a versioned manifest. For rolling-worker
compatibility, its physical v2 list keeps up to four typed `timetable_photo`
recipes ahead of the typed `text` items, while the explicit `text_first` bit
requires new workers to deliver the complete text before those optional images.
Each recipe contains only an option index; the server-owned
`assistant_message` foreign key identifies the stored, normalized presentation.
There is no arbitrary URL or message id in the JSON and no PNG/base64 blob in the
database. Progress is deliberately split: `photo_cursor`,
`photo_attempt_count`, `text_phase_started`, and the explicit `text_first`
ordering bit live in the typed JSON manifest,
while the existing database `delivery_cursor` always counts only the
legacy-compatible text list. The database `attempt_count` starts at one for
required-text delivery and resets to zero after all text is confirmed, while the
optional-photo phase uses its own bounded JSON counter. Legacy
queued payloads containing only `messages` remain readable during rollout. For
the rollback window, newly materialized manifests also retain the validated text
in that legacy `messages` key; an older worker therefore degrades to text instead
of silently treating a v2 row as empty. If that older worker sends part of the
text and a new worker later resumes the row, the new worker preserves the text
cursor, skips any now-out-of-order photos, and continues with only the unsent text.

### One renderer, not two

The image is a **screenshot of the real card**, produced by loading the real
renderer. `static/js/page-student-advisor.js` exports
`renderTimetablePresentation` as `window.__SA_RENDER_TIMETABLE_CARD__`; a
chrome-less page calls it with the stored presentation; Playwright screenshots
`#sa-card-root`.

A Pillow or matplotlib drawing routine was rejected for two reasons:

- **It would be a second renderer.** The lecture grid is already duplicated in
  four places in this codebase, and three cohort classifiers already disagree
  about `" M1"`. A fifth drawing of a week would drift from the screen the image
  links to, and a student comparing them would find them different.
- **It would re-open Arabic shaping.** Pillow does no shaping, so «الأحد» comes
  out as disconnected letters in reverse. A screenshot leaves shaping to the
  browser, and inherits the bidi fixes from #66 for free.

Inside the picture the meetings list is swapped for
`WeekGrid.renderWeekGrid({mode: 'blocks', step: 5})` — the same primitive the
planner and student timetable use, with exact five-minute geometry so a
09:00–10:15 meeting does not appear to end at 10:30. Each block prints its exact
start/end time with the course code and section. A deterministic high-contrast
palette distinguishes courses; each full course name appears once in a compact
legend instead of being repeated inside every meeting. The card uses a 720 CSS
pixel root at device scale 2 (a 1440-pixel PNG), removes the grid's outer time
padding, and uses a compact 32-pixel half-hour height for Telegram readability.
Unplaced courses keep their full reason in compact bidi-safe warning rows rather
than large red paragraphs. For a baseline card,
sections without meetings and unparseable legacy meeting strings stay in a
compact fallback list below the grid rather than disappearing. **Image only**:
the interactive chat thread keeps its semantic list.

### The card URL is signed, not session-authenticated

The headless browser has no session and must not be given one — minting a login
so a screenshot can be taken is how a convenience becomes an authentication hole.

```text
sign_card(message_id, option_index)   ->  django.core.signing, salt "telegram_gateway.card"
sign_renderer_request()               ->  separate 180-second header proof
GET /telegram/card/<signed>/          ->  requires both proofs; no session, no cookie
                                          Cache-Control: no-store, private
                                          X-Robots-Tag: noindex, nofollow
GET /telegram/card-assets/<allowlisted path>
                                      ->  renderer header required; no-store
```

Nothing mints one of these for a user; the only caller is the card renderer. A
durable worker is a separate Render service, so it cannot use the web service's
loopback address. Without an explicit valid loopback override, each render batch
therefore starts a short-lived Django origin bound to `127.0.0.1` on an ephemeral
port, renders all remaining options through one browser, and shuts the origin
down in `finally`. The wrapper exposes only the signed card route and the exact
renderer asset namespace; the asset view resolves a small filename allowlist
from source files, so a stale collected-static manifest cannot break every image.
Every other application route returns 404. A permanent request-log filter
redacts the signed path while preserving error diagnostics, and shutdown tracks
request threads with a bounded join so a stuck handler cannot accumulate more
listeners. The browser itself runs in a separate child process with a 60-second
hard deadline.
Only short-lived signed loopback URLs and the renderer proof cross its stdin; an
environment allowlist keeps the Django secret, database URL, Telegram token and
LLM credentials out of the Python/Playwright/Chromium process tree. Its stdout is
a bounded binary PNG protocol, stderr is discarded, and timeout or abnormal-exit
teardown reaps the complete process group, including a browser stuck during
`close()` or orphaned after its Python driver exits. The
browser fetches over **worker-local IPv4 loopback** — never
`TELEGRAM_PUBLIC_BASE_URL` — so the signed URL never crosses the public edge.

### Durable retry without rerunning the adviser

The answer is generated, validated and stored before anything is drawn. A
transient render or `sendPhoto` failure requeues the job at the current image
item; confirmed images are not repeated during an ordinary retry and the model is
never called again. Photo attempts and required-text attempts have independent
budgets, so three image attempts cannot consume the answer's three delivery
attempts—even when adviser generation itself needed retries. A crash on the last
photo lease degrades the remaining images and leaves the stored text claimable,
including by the previous text-only worker during a rollback. If that older
worker has already begun text delivery, rolling forward preserves its consumed
database retry count; compatibility recovery never grants extra text attempts.
Telegram exposes no
outbound idempotency key, so the same at-least-once
caveat as text still applies: a process death after Telegram accepts an image but
before the photo-cursor commit can duplicate that one image.

An image must still never cost the student the validated answer. A permanent
photo failure, or exhaustion of the bounded retry budget, skips the affected
photo item(s), records the sanitized terminal warning
`image_delivery_degraded`, and continues with the text and authenticated web
link. `transport.send_photo` returns structured, sanitized outcomes so neither
exception details nor Telegram response bodies enter the durable payload.

### Captions are 1024 characters, not 4096

So the caveats never ride in one. Images go first, the validated answer text goes
as its own message. A truncated «مقترحات للتخطيط فقط» is the failure this channel
exists to avoid.

### Two traps this feature already fell into

Both were silent, and both are now pinned by tests:

- **`{{ option_index|default:"-1" }}` swallowed option zero.** Django's `default`
  filter fires on any *falsy* value and the first option's index is `0`, so the
  first image of every answer fell back to "render as the screen does" — all
  options shown, grid swap skipped. Options 1 and 2 were correct, which is why it
  looked like it worked.
- **Multi-line `{# #}` is not stripped by Django.** All five leaked verbatim into
  the served HTML, and the one inside `<script>` produced `SyntaxError: Invalid or
  unexpected token` and a screenshot timeout. Use `{% comment %}`.

### Settings

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_SEND_TIMETABLE_IMAGES` | `false` | Explicit privacy/operations switch. Set on `advisor-system`; the worker inherits the exact value through `fromService` |
| `TELEGRAM_SEND_GRADUATION_IMAGES` | `false` | Independent opt-in for the broader graduation-progress map; never implied by the timetable flag |
| `TELEGRAM_INTERNAL_BASE_URL` | `""` | Optional plain-HTTP IPv4-loopback override (`127.0.0.1` or `localhost`) for local development. Empty uses the worker's short-lived loopback origin; `::1` is rejected |

### Production: self-contained worker rendering

The durable worker renders against its own temporary loopback origin. Do not point
`TELEGRAM_INTERNAL_BASE_URL` at the public site as an ad-hoc workaround: that
would send a bearer card token through the public edge and make availability of
the image depend on another service.

`build.sh` has run `playwright install chromium` since 2026-04-08. An earlier
draft of this document said the opposite, and that error was worse than useless:
it told an operator to expect exactly the symptom the real bugs produced, so
`card render failed` would have been explained away instead of investigated.

Two production hazards still apply to the worker-local **loopback fetch**, and
both are pinned by tests:

- `ALLOWED_HOSTS` is built purely from `DJANGO_ALLOWED_HOSTS`, which an operator
  sets to the public hostname — so `Host: 127.0.0.1:PORT` was a **400
  DisallowedHost**. `config/settings.py` now appends the loopback hosts whenever
  the image flag is on.
- `SECURE_SSL_REDIRECT` is on whenever `DEBUG` is off, so the plain-HTTP loopback
  fetch was **301'd to `https://127.0.0.1:PORT`**, where nothing speaks TLS. The
  renderer now sends `X-Forwarded-Proto: https`, which is what the edge asserts in
  production. Exempting the card page alone would not have worked: its private
  CSS/JavaScript asset requests pass through the same middleware, so the renderer
  script would not load and the page would report `renderer-missing`.

Both used to be invisible. Framework request diagnostics now remain visible with
the signed path permanently redacted, while child-process failures are reported
only by sanitized categories and never include a URL, environment value, or
browser output.

### Housekeeping

```bash
python manage.py purge_telegram_tokens --apply
```

Deletes spent link tokens and old **terminal** update/job rows. Durable rows age
from `finished_at`, so time spent waiting or running does not consume their
retention window. Legacy inline receipts with no finish timestamp use
`received_at`. It never deletes `QUEUED` or `RUNNING` work. The command is dry-run
without `--apply`; `render.yaml` runs the applied cleanup every day after
`purge_planner_drafts`, because retention must not depend on deploy frequency.

---

## 7. Migrations

The final channel spans one core migration and three gateway migrations:

- `telegram_gateway/0001_initial.py` creates the isolated link, token and update-
  receipt tables. It depends on
  `core.0061_scope_student_term_section_uniqueness`.
- `core/0062_advisor_message_generation_profile.py` adds the server-owned
  `AdvisorMessage.generation_profile` provenance marker. Existing rows receive
  the empty/unprofiled value and therefore cannot become Telegram-safe history.
- `telegram_gateway/0002_durable_advisor_jobs.py` extends the original update
  receipt into the durable queue envelope, adds its foreign keys, finish/lease/
  delivery fields, FIFO indexes and one-running-job-per-link constraint.
- `telegram_gateway/0003_account_binding.py` binds a link and an approved token to
  the exact university user account. Because legacy rows contain no historical
  account primary key that can be verified safely, its data migration revokes all
  active legacy links and burns approved-but-unconsumed legacy tokens. Those users
  must complete the linking ceremony again.

The rollout-compatible fields in `0002` have **database defaults**, not merely
Python defaults. An old web process that still inserts the original
`(update_id, received_at)` shape therefore creates an `INLINE` / `SUCCEEDED`
terminal receipt with empty/zero queue fields; it neither fails on missing columns
nor accidentally queues historical traffic. Nullable relationship and lease
fields complete that rolling-deploy compatibility. A direct-SQL regression test
pins the old insert shape.

The gateway owns:

- `telegram_links` — two partial unique indexes (`uq_tg_active_telegram_user`,
  `uq_tg_active_student`), both `WHERE status='ACTIVE'`
- `telegram_link_tokens`
- `telegram_update_receipts`

Run the unscoped migration command before starting the worker. Targeting only the
`telegram_gateway` app does **not** guarantee that the independent core `0062`
branch is applied.

```bash
.venv/Scripts/python.exe manage.py migrate                          # apply all four
.venv/Scripts/python.exe manage.py migrate telegram_gateway zero    # remove channel tables
```

The second command leaves the harmless core provenance column in place; it is a
schema rollback, not operational queue cleanup, and must follow an application
rollback that no longer runs the gateway.

> Partial unique indexes are enforced by both SQLite and PostgreSQL 16. Index
> names are global in PostgreSQL — these are prefixed `uq_tg_` and do not collide.

---

## 8. Reliability

The webhook separates two acknowledgements. A linked question or ordered command
is first committed to PostgreSQL; the `update_id` is both its idempotency key and
durable work row, so a webhook retry cannot enqueue a second model call. For a
question, the row initially carries a short future `available_at` timestamp. Only
after that commit does the request attempt the bilingual “Received — preparing…”
progress message. The job is released to the worker in a `finally` block after
that attempt, so the final answer cannot overtake the progress message. If the web
process dies before release, the original bounded timestamp makes the job
claimable automatically. A lost progress message never suppresses the final
answer. HTTP 200 follows the durable enqueue and progress-message attempt.

Render runs one persistent queue process:

```bash
python manage.py telegram_advisor_worker --sleep 1 --max-attempts 3
```

The worker leases ready rows, processes each link in order, and materialises the
answer/command result before contacting Telegram. Accepted photos advance the
manifest's JSON photo cursor, and image attempts advance its separate JSON retry
counter; accepted text chunks advance the database text cursor and use the
database retry counter. A process exit does not erase work: an expired 30-minute default lease
becomes claimable again. A retry resumes the materialised payload at the first
unconfirmed item; it does not call the model, create a new conversation, or repeat
a command side effect. Retryable failures are requeued up to the configured
attempt limit; terminal failures remain inspectable until the retention job
removes them. `QUEUED` and `RUNNING` rows are never retention-cleanup candidates.

A 2xx Bot API response counts as delivery only when its JSON object contains
`"ok": true`; HTTP 200 with `"ok": false`, invalid JSON, network errors and 5xx
responses remain failures and are requeued up to the attempt cap. HTTP 429 is
also retryable and, when Telegram supplies a valid integer
`parameters.retry_after`, that exact validated delay becomes the job's next
`available_at`. Other 4xx responses are permanent: a text-item rejection fails the
job, while a photo-item rejection takes the documented image-degradation path and
still releases the answer text. Remote descriptions, message objects, file
metadata and response bodies are neither logged nor persisted.

Outbound delivery is intentionally **at least once**, not exactly once. The
cursor can be advanced only after Telegram accepts a send. If the worker dies in
the narrow gap after that acceptance but before the database update commits, the
replacement worker sends that message again. Telegram exposes no idempotency key
for `sendMessage` or `sendPhoto`; the safe trade is a possible duplicate text or
image rather than a silently lost answer. In this delivery-only crash window the
already-materialised generation is not rerun, and `/advisor` does not create a
second case.

`/advisor` has one additional transaction boundary. The escalation side effect
and the durable Telegram reply payload containing its case reference commit in
the same database transaction under the job lease. The network send happens only
after commit. A crash can therefore retry the stored reply without creating a
second escalation—even if a human closes the first case before the retry.

There is no Redis/Celery dependency. PostgreSQL is already the system of record,
and the lease is safe across deploys and worker restarts. Keep exactly one Render
worker for predictable ordering and capacity; the database claims remain the
correctness boundary if a replacement overlaps briefly during a deploy.

`advisor-system` is the single source of truth for the Telegram token/public
origin/link TTL/API timeout/image switch and selected `LLM_*`,
`VIRTUAL_ADVISOR_*`, and `STUDENT_ADVISOR_V2_*` values. `render.yaml` copies the
non-empty runtime values into the worker with `fromService`; do not create an
independently editable worker copy. The optional empty `TELEGRAM_INTERNAL_BASE_URL`
and `LOCAL_LLM_MODEL` are deliberately omitted from the worker because Render
cannot resolve blank cross-service values; Django's empty defaults preserve the
worker's loopback renderer and disabled local-model behavior. Render ignores newly
added `sync: false` variables when updating an
existing Blueprint, so populate and verify every declared value on
`advisor-system` before that sync. Set either image flag to true only after its
disclosure and image smoke tests in §§9–11 are in place.
`STUDENT_ADVISOR_V2_ENABLED` still controls the web rollout; it does not downgrade
`telegram_safe`, while the V2 iteration/call/token/timeout controls do govern the
Telegram turn.

The worker validates configuration before its first queue query. It refuses to
start unless the channel is enabled, the Bot API token is syntactically usable,
`TELEGRAM_PUBLIC_BASE_URL` is one credential-free HTTPS origin on a Telegram
webhook port, and the selected production LLM client can be constructed with its
egress approval enabled. Provider validation opens no socket; provider
reachability is still covered by the deployment smoke test. When either adviser
image switch is enabled, a separate preflight resolves the exact source assets,
performs an authenticated bounded request against the worker-local card origin,
and launches/closes Chromium in a secret-stripped subprocess. A hard timeout
terminates its process tree, and a sanitized preflight failure stops the worker
before it claims a student question. This channel-wide fail-fast state is
deliberate when images are explicitly enabled: the service must show as unhealthy
and leave durable jobs queued instead of silently operating as text-only. Once a
healthy worker has started, an individual card's runtime/render/send failure still
uses the per-job text fallback described in §6.

---

## 9. Production deployment checklist

- [ ] Exact GPA / marks / failed-course requests return the authenticated-portal boundary message (§4)
- [ ] `TELEGRAM_WEBHOOK_SECRET` generated with a CSPRNG, ≥ 32 chars, set in Render
      env (`sync: false`), **not** in the repo
- [ ] `TELEGRAM_BOT_TOKEN` set in Render env, never committed
- [ ] `TELEGRAM_PUBLIC_BASE_URL` is the real **https://** origin
- [ ] All non-empty worker-consumed Telegram/adviser variables are populated on
      `advisor-system`; the worker receives them through `fromService`. The two
      documented optional empty values remain absent from the worker.
- [ ] Unscoped `manage.py migrate` applied (`core.0062` and
      `telegram_gateway.0001`–`0003` all present)
- [ ] Any pre-`0003` active links have been revoked by the migration and are
      re-approved through the two-sided ceremony, not manually reactivated
- [ ] Persistent `advisor-telegram-worker` is running the command in §8
- [ ] `/privacy` and the pre-link confirmation disclose timetable-image contents
      and, if enabled, graduation-map course states, prerequisite links, scenario
      changes and unresolved requirements; plus Telegram retention/forwarding and
      that unlink cannot retract sent media
- [ ] Initial production rollout is text-only:
      `TELEGRAM_SEND_TIMETABLE_IMAGES=false` and
      `TELEGRAM_SEND_GRADUATION_IMAGES=false` are set on `advisor-system` and
      inherited by the worker. Leave `TELEGRAM_INTERNAL_BASE_URL` empty. Enable
      either image switch only in a separately reviewed rollout after the worker
      runtime passes the Chromium/Playwright preflight.
- [ ] `TELEGRAM_ADVISOR_ENABLED=true` set on `advisor-system`; Blueprint synced so
      the worker inherits it, and both services restarted
- [ ] `python manage.py telegram_webhook --set` completed
- [ ] `python manage.py telegram_webhook --info` shows `max_connections: 1` and no
      `last_error_message`
- [ ] BotFather privacy mode **enabled**, join-groups **disabled**
- [ ] Daily Render retention cron includes `purge_telegram_tokens --apply`
- [ ] If ordinary student-portal or Advisor V2 OTP testing needs one operator
      inbox, complete this entire temporary **global** redirect cycle in one
      controlled window:
  1. Immediately before entering the receiver value, record the authoritative
     UTC start from the running web service as `global_redirect_started_at`:
     `python manage.py shell -c "from django.utils import timezone; print(timezone.now().isoformat())"`.
     Also record the intended test student ID(s) for the execution audit; cleanup
     remains window-wide regardless. Then set `STUDENT_OTP_REDIRECT_EMAIL`
     manually on the **`advisor-system` web service only**. `render.yaml`
     intentionally declares the key as `sync:false`; never add/commit the receiver
     value to the Blueprint, source control, a shared local env file, the worker,
     or the cron service.
  2. Deploy/restart the web service and verify the running setting is non-empty.
     While set, **every** student login OTP is redirected, so keep the window
     short and exercise only the recorded test IDs. Verify both portal login and
     authenticated Advisor V2 access.
  3. Immediately clear/remove the setting in Render and deploy/restart the web
     service again.
  4. Only after the empty-setting redeploy is live, verify the running web process
     reports it empty and record the returned authoritative UTC end as
     `global_redirect_ended_at`:
     `python manage.py shell -c "from django.conf import settings; from django.utils import timezone; assert not settings.STUDENT_OTP_REDIRECT_EMAIL; print(timezone.now().isoformat())"`.
  5. Because the global redirect affected any login during that effective
     interval, invalidate **every** still-unconsumed OTP created within the exact
     inclusive UTC window. Do not restrict this cleanup to the planned test IDs
     (replace both fail-closed placeholders with the recorded values):

     ```python
     python manage.py shell
     >>> from datetime import datetime
     >>> from django.utils import timezone
     >>> from core.models import StudentLoginOTP
     >>> started_at = datetime.fromisoformat("<GLOBAL_REDIRECT_STARTED_AT>")
     >>> ended_at = datetime.fromisoformat("<GLOBAL_REDIRECT_ENDED_AT>")
     >>> assert timezone.is_aware(started_at) and timezone.is_aware(ended_at)
     >>> assert started_at <= ended_at
     >>> StudentLoginOTP.objects.filter(
     ...     consumed=False,
     ...     created_at__gte=started_at,
     ...     created_at__lte=ended_at,
     ... ).update(consumed=True)
     ```
- [ ] If the link-only OTP receiver is needed for acceptance testing, complete
      this entire temporary-control cycle; never leave it half-finished:
  1. Set `TELEGRAM_LINK_OTP_REDIRECT_EMAIL` manually on the
     **`advisor-system` web service only**, and record the exact acceptance-test
     student ID(s). `render.yaml` intentionally declares the key as `sync:false`;
     never add/commit the receiver **value** to the Blueprint, source control, a
     shared local env file, the worker, or the cron service.
  2. Deploy/restart the web service, verify the setting is non-empty in that web
     process, and run only the Telegram linking OTP test. Ordinary login must
     still address the student's university mailbox.
  3. Immediately clear/remove the setting in Render and deploy/restart the web
     service again.
  4. Verify the running web process reports it empty:
     `python manage.py shell -c "from django.conf import settings; assert not settings.TELEGRAM_LINK_OTP_REDIRECT_EMAIL; print('empty')"`.
  5. Invalidate still-unconsumed codes for only the recorded acceptance-test
     student ID(s). Never invalidate other students' login attempts. OTP rows do
     not retain which receiver was used, so the operator must supply the IDs
     recorded in step 1 (replace the fail-closed placeholder before running):

     ```python
     python manage.py shell
     >>> from core.models import StudentLoginOTP
     >>> test_student_ids = [<TEST_STUDENT_ID>]  # exact ID(s) recorded in step 1
     >>> StudentLoginOTP.objects.filter(
     ...     student_id__in=test_student_ids, consumed=False
     ... ).update(consumed=True)
     ```
- [ ] Smoke test with a test bot and a test student (§10)
- [ ] Text-only smoke test returns the complete, untruncated answer and sends no
      photo. Image ordering tests belong to the separately approved image rollout.

## 10. Rollback / disable

**Fastest (seconds), no deploy:** set `TELEGRAM_ADVISOR_ENABLED=false` on the web
service, restart it, and suspend `advisor-telegram-worker`. The webhook then
answers 404 and no queued answer is delivered. Queued rows remain durable for a
controlled recovery; do not purge them as part of rollback.

If an OTP-redirect acceptance test is interrupted, first clear/remove both
redirect settings from the web service and deploy/restart. Cleanup then depends
on which control was effective:

- **Global `STUDENT_OTP_REDIRECT_EMAIL`:** after runtime verification that it is
  empty, record the authoritative UTC end and invalidate every unconsumed OTP in
  the inclusive `global_redirect_started_at` → `global_redirect_ended_at` window
  using §9. If the exact start was not preserved, choose a conservative earlier
  UTC bound that certainly covers when the value could first have become
  effective; never narrow global cleanup to the intended test IDs.
- **Link-only `TELEGRAM_LINK_OTP_REDIRECT_EMAIL`:** after runtime verification
  that it is empty, invalidate unconsumed OTPs only for the exact acceptance-test
  student ID(s), using the scoped command in §9.

This cleanup is required even when the Telegram channel itself is being disabled.

For a broader adviser/provider rollback, also set
`STUDENT_ADVISOR_V2_ENABLED=false` and
`ALIBABA_LLM_ALLOW_LIVE_REQUESTS=false`. Do **not** sync the Blueprint after an
emergency dashboard disable: the checked-in rollout contract enables these three
switches and a sync would turn them back on. Revert the switches in `render.yaml`
and its contract tests first, merge that rollback, and only then sync again.

**Stop Telegram calling at all:**

```bash
python manage.py telegram_webhook --delete
```

**Kill the credential:** BotFather → `/revoke`. Every call with the old token dies.

**Revoke one student's access:** Django admin → Telegram links → select → *Revoke
the selected Telegram links*. Or `linking.revoke_links_for_student(student_id)`.

**Full channel removal:** after rolling back code that starts or routes the
gateway, run `migrate telegram_gateway zero`, then remove `telegram_gateway` from
`INSTALLED_APPS` and `config/urls.py`. The shared turn service, the core channel-
privacy helpers and `AdvisorMessage.generation_profile` remain; removing that core
provenance field would require its own reviewed migration. In particular,
`core/services/advisor_turn.py` **stays** because the web adviser uses it.

---

## 11. Manual test script (test bot + test student)

Use a **separate** BotFather bot and a test student. Never the production bot.

| # | Action | Expected |
|---|---|---|
| 1 | `/start` in a private chat | Welcome + how to link. No student data |
| 2 | `كم معدلي؟` before linking | Linking instructions only. No record, no student number |
| 3 | `/privacy` | Notice incl. "not end-to-end encrypted"; timetable and graduation-map image contents; forwardability; and what unlink cannot delete |
| 4 | `/link` | A single-use URL with a stated expiry, no identifiers in it |
| 5 | Open it signed out | Lands in the **existing** student login (Uni ID → OTP) |
| 6 | Complete login | Returns to the confirmation page, privacy text visible |
| 7 | Approve | Web shows a `/confirm <code>`; **nothing is linked yet**; the chat gets NO code |
| 7b | Send `/confirm <code>` in the chat | Bot confirms the link |
| 7c | **Forwarded-link drill**: mint a token in bot chat A, open the URL in a browser signed in as the test student, approve, then try `/confirm <code>` from a *different* chat B | Refused. Nothing linked |
| 8 | Reopen the same URL | "expired or already used" |
| 9 | Ask `ما المواد المتبقية لي؟` | Brief ack, then a real Arabic answer about **this** student |
| 9b | Ask `كم معدلي؟` after linking | No model answer; directs the student to the authenticated web adviser |
| 9c | Ask to see the current/expected timetable, then ask for a proposed timetable | The complete text answer and authenticated web link arrive first; then one baseline image for the first request and one image per generated option (maximum four) for the second |
| 9d | With the graduation-image flag enabled, ask `كم فصل باقي لي؟` | The complete text arrives first, followed by one graduation-plan image; the map labels the planning baseline and remains read-only |
| 10 | Compare with the web adviser | Same student and stored thread; Telegram still exposes only its reduced evidence profile |
| 11 | Follow-up (`وماذا عن الفصل القادم؟`) | Understands the prior **safe Telegram** answer without ingesting web/withheld turns |
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

## 12. Known limitations

1. **Only timetable presentations become images.** Other adviser presentations
   remain text-and-authenticated-web-link, and a timetable response is capped at
   four option images.
2. **Durability still needs a running worker.** A stopped worker does not lose
   committed work, but students receive no queued answers until it resumes.
3. **Answer messages use plain text.** No bold, no text tables, no inline keyboards
   or buttons — the deliberate cost of having no escaping bug. Timetable cards are
   the one image type.
4. **Bidi: the card image is correct, the text may not be.** The screenshot inherits
   the template-layer fix from #66 — «09:00-10:15» renders the right way round,
   verified. The plain-text answer is a different matter: Telegram applies its own
   bidi to message text, and no Telegram-specific isolation pass was written.
   Check a text answer containing a time range on a real client.
5. **English sentences inside the Arabic card reverse their punctuation** —
   «.the registration portal». Pre-existing on the web card, not caused by the
   images: at `page-student-advisor.js:1240-1245` the course *code* is isolated
   with `ltrNode` while the course *name* and the English `reason` beside it are
   not. Not fixed here. → the bidi topic file.
6. **One conversation per chat.** No thread switching from Telegram; `/new` is the
   only control. The web sidebar remains the full interface.
7. **`/advisor` escalates the most recent answered turn only.**
8. **Outbound retry is bounded.** Transient failures and 429 responses are retried;
   after three claims a text failure becomes terminal, while an exhausted photo
   falls back to the validated text and web link. Any generated answer remains
   visible on the web for investigation.
9. **Outbound delivery is at least once.** A crash after Telegram accepts a text
   message or photo but before its cursor update can produce one duplicate item on
   retry. The model and durable command side effects are not repeated.
10. **No delivery of adviser replies to escalations.** A resolved case is not
    pushed to Telegram; the student checks the platform.
11. **Terminal-job history is retained for seven days after completion.** The
    daily Render cron ages durable rows from `finished_at` and legacy inline rows
    from `received_at`; export separate audit data before then if required.
12. **The `csrf_exempt` webhook is the repo's second.** It reads no session and no
    cookie, but it is a permanent exception worth re-reviewing if it ever grows.

---

## 13. Implementation map

The implementation is spread deliberately across the shared adviser boundary and
the transport-specific gateway:

| File / area | Responsibility |
|---|---|
| `core/services/advisor_turn.py` | Channel-neutral turn ordering, persistence, idempotency and profiled-history selection |
| `core/services/advisor_channel_privacy.py` | Telegram-safe schemas, evidence projection, safe fallback, system rules and history filter |
| `core/services/advisor_history.py` | Visible and server-profiled conversation-history loaders |
| `core/services/student_advisor_v2.py` | Forced V2 selection for `telegram_safe` and projection hooks around tools/fallback |
| `core/models.py`, `core/migrations/0062_…` | Server-owned `AdvisorMessage.generation_profile` provenance |
| `telegram_gateway/bot.py` | Parsing, command meaning, intent gate, output DLP and durable executor |
| `telegram_gateway/jobs.py` | Admission, FIFO leases, typed text/photo manifest, delivery cursor, retry and recovery |
| `telegram_gateway/transport.py` | Sanitised Bot API client and `ok`/HTTP/429 interpretation |
| `telegram_gateway/rendering.py`, `render_child.py`, `cards.py` | Worker-local card origin, bounded credential-free screenshot child and short-lived signed render references |
| `telegram_gateway/linking.py`, `models.py` | Two-sided linking, exact account revalidation, retention query and channel data model |
| `telegram_gateway/views.py` | Webhook authentication, durable enqueue and ordered progress acknowledgement |
| `telegram_gateway/configuration.py`, `management/commands/` | Startup/webhook validation, persistent worker and housekeeping commands |
| `telegram_gateway/migrations/0001_…`–`0003_…` | Channel tables, durable queue rollout and exact-account migration |
| `telegram_gateway/templates/`, `urls.py`, `admin.py` | Browser ceremony, routing and restricted administration |
| `render.yaml` | Web/worker environment inheritance and scheduled retention |

Regression coverage lives across `tests/test_telegram_gateway.py`,
`test_telegram_jobs.py`, `test_telegram_transport.py`,
`test_telegram_account_lifecycle.py`, `test_advisor_channel_privacy.py`, and the
shared adviser conversation/escalation suites. Counts are intentionally omitted:
the suite grows as review findings become executable boundaries.

### The review that changed the design

A four-lens adversarial review (authentication, webhook/transport, extraction
fidelity and mutation testing) ran against the first implementation. It found the
channel-binding blocker described in §2, plus an unauthenticated non-ASCII-header
500, mid-generation revocation leakage, request-thread blocking, crash-burned
receipts and a stale post-login redirect. Later production reviews added the
durable queue, exact-account binding, forced reduced evidence profile, output DLP,
progress ordering, sanitised Bot API retry semantics and atomic `/advisor`
materialisation described above.

Each material boundary has a focused regression test, with mutation checks used
for the original review findings. The shared web application-service ordering
remains in `advisor_turn`; Telegram-specific trust decisions stay at explicit
profile and transport boundaries.
