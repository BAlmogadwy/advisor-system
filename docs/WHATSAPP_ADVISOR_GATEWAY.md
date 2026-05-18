# WhatsApp Advisor Gateway Design

## Purpose

Expose the virtual academic advisor through WhatsApp without weakening privacy,
authorization, or auditability. WhatsApp is treated as a delivery channel only.
The system must verify the sender, link the WhatsApp identity to a university
identity, and enforce the same role scope used by the web application before
any academic data is queried or summarized.

## Non-Negotiable Security Principles

1. A WhatsApp phone number is not authentication.
2. The LLM never decides authorization.
3. Every data query runs under a server-built scope.
4. Student users can only access their own record.
5. Advisor users can only access their assigned students.
6. General academic advisors can only access assigned departments.
7. Super admin access over WhatsApp is disabled by default or requires a
   stronger step-up flow before production enablement.
8. Group chat messages must not return student records.
9. OTP values are never stored in plaintext.
10. Every link, unlink, OTP failure, sensitive query, and advisor query is
    auditable.

## External Integration Boundary

Meta WhatsApp Cloud API webhooks require a publicly reachable HTTPS endpoint,
a webhook verification token, and a `200` response for accepted notifications.
The Meta-hosted WhatsApp SDK docs describe the webhook listener pattern, the
verification token, and the need for HTTPS webhook hosting:
https://whatsapp.github.io/WhatsApp-Nodejs-SDK/receivingMessages/

The Django app should expose only a narrow gateway:

```text
Meta WhatsApp Cloud API
-> whatsapp_gateway webhook
-> identity/session/OTP layer
-> RBAC scope builder
-> existing virtual advisor service
-> verified DB tools
-> WhatsApp reply
```

No WhatsApp code should be placed inside the existing `virtual_advisor_views`
page code. The web chat and WhatsApp chat share the advisor core, but each
channel owns its own authentication and transport concerns.

## Authentication Flow

Unified linking flow for students, advisors, and staff:

```text
1. User sends a message to the official WhatsApp bot.
2. Gateway receives `wa_id` and sender phone.
3. If no active link exists, the bot asks for the university ID.
4. System resolves the university ID to a student/advisor/staff record.
5. System sends an OTP to the registered university email.
6. User replies with the OTP in WhatsApp.
7. System verifies the OTP and creates an active WhatsApp link.
8. Future messages use the linked role scope.
```

The flow is intentionally shared. The difference is the scope after linking:

```text
student -> {"role": "STUDENT", "student_id": ...}
advisor -> {"role": "ADVISOR", "advisor_id": ...}
general advisor -> {"role": "GENERAL_ACADEMIC_ADVISOR", "departments": [...]}
super admin -> denied on WhatsApp unless explicitly enabled later
```

## Current Data Dependency

The current local DB has advisor email in `academic_advisors.email`, but the
`students` table does not currently have a student email column. Production
student linking therefore needs one of these before enabling student OTP:

- add/import a verified student email column;
- maintain a separate `student_contact_methods` table;
- configure a university-issued deterministic email pattern only if the
  institution guarantees it.

The implementation foundation keeps the resolver explicit and fails closed if
student email cannot be resolved.

## Step-Up Authentication

The initial link is not enough forever. Require fresh OTP when:

- linking a new WhatsApp number;
- user has been inactive past the configured auth freshness window;
- advisor asks for large lists or exports;
- user requests highly sensitive personal academic details;
- unusual rate or query patterns are detected.

Suggested defaults:

```text
OTP TTL: 5 minutes
OTP attempts: 5
Student session freshness: 30 days for low-risk own-record questions
Advisor session freshness: 24 hours for list queries
Large list/export freshness: fresh OTP within 10 minutes
```

## Data Model

```text
whatsapp_user_links
- id
- wa_id unique
- phone_number
- role
- status: active/revoked/locked
- user_id nullable
- student_id nullable
- advisor_id
- departments
- verified_at
- last_seen_at
- revoked_at
- created_at
- updated_at
```

```text
whatsapp_otp_challenges
- id
- wa_id
- phone_number
- university_id
- resolved_role
- resolved_user_id nullable
- resolved_student_id nullable
- resolved_advisor_id
- resolved_departments
- email_masked
- otp_hash
- expires_at
- attempts
- status: pending/verified/expired/locked
- created_at
- verified_at
```

```text
whatsapp_conversations
- id
- wa_id unique
- state
- last_auth_at
- last_message_at
- step_up_required
- created_at
- updated_at
```

```text
whatsapp_message_logs
- id
- wa_id
- direction: inbound/outbound
- message_type
- text_preview
- status
- provider_message_id
- created_at
```

## Message Handling Rules

### Unknown sender

```text
User: hello
Bot: Please send your university ID to link WhatsApp.
```

### Pending OTP

```text
User: 123456
Bot: WhatsApp linked successfully. You can now ask academic questions.
```

### Authenticated student

```text
User: What is my GPA and remaining courses?
System: call virtual advisor with student_id locked to this link.
```

### Authenticated advisor

```text
User: Show my AI students with earned credits above 85.
System: call virtual advisor with advisor scope only.
```

### Group chat

```text
System: refuse student data and suggest one-to-one chat.
```

## Implementation Phases

### Phase 1: Foundation

- Add separate `whatsapp_gateway` Django app.
- Add models and migrations.
- Add webhook verification endpoint.
- Add signature verification helper.
- Add OTP creation/verification service.
- Add scope builder for active links.
- Add tests for linking, OTP expiry, and scope enforcement.

### Phase 2: Controlled Pilot

- Configure Meta webhook credentials in environment variables.
- Configure verified student email source.
- Send/receive plain text.
- Pilot with internal staff and test student records.
- Keep super admin disabled.

### Phase 3: Advisor Core

- Connect active advisor links to the existing virtual advisor.
- Enforce advisor and department scope in all DB tools.
- Add list-size limits and step-up OTP.
- Add advisor audit views.

### Phase 4: Student Core

- Enable student own-record questions.
- Add safe answer templates for GPA, remaining courses, registration status,
  and course recommendations.
- Add refusal messages for out-of-scope requests.

### Phase 5: Production Hardening

- Move webhook processing to a background queue.
- Add outbound retry handling.
- Add provider message idempotency.
- Add admin dashboard for linked WhatsApp identities.
- Add monitoring for OTP abuse, advisor bulk queries, and failed signatures.

## Review Checklist Before Production

- Meta webhook uses HTTPS.
- Webhook verification token is long and secret.
- Payload signatures are verified when app secret is configured.
- Access token is not committed to the repo.
- OTP is hashed with server secret.
- Student email source is verified.
- Super admin WhatsApp access is disabled.
- Group chats cannot return academic records.
- Rate limits are enabled.
- Audit entries exist for all sensitive events.
- LLM prompt cannot override server scope.
