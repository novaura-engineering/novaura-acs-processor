# Mailgun and email capabilities (ACS and communications)

This document summarizes how **Mailgun** and **Postmark** are integrated for outbound email, optional **domain template sync**, and **inbound / event webhooks** across the **communications**, **ACS** (account configuration / nurturing), **CRM** (lead actions), and **communication_processor** Django apps.

---

## 1. Architecture at a glance

| Layer | Role |
|-------|------|
| **communications** | `ContactEndpoint` + `ContactEndpointEmailSettings` (provider, non-secret `config`, Secrets Manager ARN). Provider registry resolves **`MailgunEmailAdapter`** or **`PostmarkEmailAdapter`**. `email_dispatch` loads credentials and calls `adapter.send`. |
| **communications** (API) | Authenticated **`POST /api/communications/send-contact-email/`** sends arbitrary HTML/text using the endpoint’s configured provider (Mailgun when `provider=mailgun`). |
| **ACS** | **`EmailConfig`** chooses how body/subject are built (`inline` vs **`outbound_acs`**). **`OutboundEmailTemplate` / `OutboundEmailTemplateVersion`** hold ACS-owned HTML with approval workflow; optional **push** copies approved versions to Mailgun domain templates. **Bulk nurturing** sends use `send_from_email_config` → same Mailgun transport. |
| **CRM** | Lead **`POST …/actions/send_email/`** uses **environment-variable Mailgun** (`send_via_env_mailgun_fallback`) when no contact endpoint is involved. |
| **communication_processor** | **`POST …/webhooks/mailgun/email/`** verifies Mailgun signatures, persists **`EmailMessage`**, enqueues **SQS** for downstream processing. |

**Important product note:** Normal **outbound_acs** sends merge HTML server-side and call **`send_from_contact_endpoint`** (raw Mailgun `messages` API). **Sync to Mailgun** is optional (copy for operators or future `send_template` flows); bulk sends do not require `mailgun_template_name` to be set. See `docs/FRONTEND_OUTBOUND_EMAIL_UI_ALIGNMENT_CHECKLIST.md`.

---

## 2. Communications app

### 2.1 Data model: `ContactEndpointEmailSettings`

- **One-to-one** with `ContactEndpoint` (`related_name='email_settings'`).
- **`provider`:** `mailgun`, `postmark` and `mailchimp_transactional` are implemented outbound providers. `resend` and `mailchimp_marketing` exist in model choices without a send adapter — **Mailchimp Marketing is audience/campaign-grain and cannot do the per-recipient sends this pipeline is built on**, so use `mailchimp_transactional` to send through a Mailchimp account.
- **`config` (JSON, non-secret):** For Mailgun, validated keys include:
  - **`domain`** — sending domain (also overridable via secret `domain`).
  - **`eu_region`** (boolean) — selects EU vs US Mailgun API base (`api.eu.mailgun.net` vs `api.mailgun.net`).
  - **`list_unsubscribe_mailto`** (optional) — mailto target or full `mailto:…` URI for `List-Unsubscribe`.
  - **`list_unsubscribe_https`** (optional) — HTTPS unsubscribe URL for `List-Unsubscribe` (and RFC 8058 one-click when enabled).
  - **`list_unsubscribe_one_click`** (optional boolean) — when `true` and an HTTPS list-unsubscribe URI is present, adds `List-Unsubscribe-Post: List-Unsubscribe=One-Click`. When omitted, one-click defaults to **on** if an HTTPS URL is configured (and is **on** when both mailto and HTTPS are set). Set **`false`** to omit the Post header.
  - If these keys are absent, optional Django settings **`LIST_UNSUBSCRIBE_MAILTO`** / **`LIST_UNSUBSCRIBE_HTTPS`** (environment-driven) supply the same values for ECS or ops defaults without CRM UI. **URLs and mailboxes must be live endpoints** that honor unsubscribe; tokenized per-recipient URLs require matching server-side behavior (Mailgun `%recipient%` placeholders only help if your HTTPS handler implements them).
- **`credentials_secret_arn` / `credentials_secret_region`:** AWS Secrets Manager JSON typically includes:
  - **`api_key`** (or legacy **`MAILGUN_API_KEY`**)
  - Optional **`webhook_signing_key`** / **`MAILGUN_WEBHOOK_SIGNING_KEY`** for webhook verification

Public helper: **`load_credentials_for_email_settings`** (used by webhooks and dispatch).

### 2.2 `MailgunEmailAdapter` (`shared_services/email/mailgun.py`)

- **`send`** — `POST /v3/{domain}/messages` with `from`, `to`, `subject`, `html`, `text`, optional `h:Reply-To`, optional list-unsubscribe `h:` headers from endpoint `config` (see above), up to **3** `o:tag` entries (128 chars each).
- **Plain `text` part:** When no non-empty plain body is supplied (`text_body` is `None` or explicit `""`), the **`text`** field is generated from the final HTML with tag stripping (BeautifulSoup) so the MIME alternative matches the HTML semantically (avoids **MPART_ALT_DIFF**-style issues from sending HTML in `text/plain`). Non-empty `text_body` is sent unchanged.
- **`send_template`** — same endpoint with Mailgun **`template`** + **`t:variables`** (JSON), optional **`t:version`**, optional open/click tracking flags, optional `t:text` generation from template.
- **Domain templates (HTTP APIs used by ACS sync):**
  - Create template / first version
  - Add version (`mailgun_push_domain_template_version` — creates template on 404, else adds version)
- **`verify_webhook_request`** — HMAC verification for **form-encoded** inbound posts and **JSON** event payloads (`event-data.token|timestamp|signature`), with timestamp skew guard.
- **`parse_webhook_request`** — Normalizes inbound MIME (form) or event webhooks into `NormalizedEmailWebhookPayload`.

### 2.2a `PostmarkEmailAdapter` (`shared_services/email/postmark.py`)

- **`send`** — `POST https://api.postmarkapp.com/email` with `From`, `To`, `Subject`, `HtmlBody`, `TextBody`, optional `ReplyTo`, optional `Tag`, optional `MessageStream`, optional tracking fields, optional `Metadata`, and optional Postmark `Headers`.
- **Credentials secret JSON:** include **`api_key`** or **`server_token`** (also accepts legacy **`POSTMARK_SERVER_TOKEN`**). These values are Postmark server tokens.
- **`config` (JSON, non-secret):**
  - **`message_stream`** or **`transactional_stream`** — Postmark stream name. Defaults to **`broadcast`** for nurturing/bulk campaign safety because marketing sends should use a broadcast stream.
  - **`track_opens`** (optional boolean).
  - **`track_links`** (optional string): one of **`None`**, **`HtmlAndText`**, **`HtmlOnly`**, **`TextOnly`**, **`Subscription`**.
  - **`metadata`** (optional object): up to 10 key/value pairs after normalization.
  - **`list_unsubscribe_mailto`**, **`list_unsubscribe_https`**, **`list_unsubscribe_one_click`** use the shared dispatch path; Postmark receives them as `Headers: [{"Name": ..., "Value": ...}]`.
- **Plain `TextBody`:** When no non-empty plain body is supplied (`text_body` is `None` or explicit `""`), the final HTML is converted with the same `html_to_plain_text` helper used by Mailgun.
- **Retries:** transient `requests.Timeout`, `requests.ConnectionError`, HTTP **5xx**, and **429** are retried with bounded exponential backoff and jitter; other **4xx** errors fail immediately.
- **Webhooks:** Postmark webhook verification and event normalization are not implemented in this ACS processor. Add Postmark webhook ingestion in `novaura_crm_backend/communication_processor/views/` alongside the existing Mailgun webhook path.

### 2.2b `MailchimpTransactionalEmailAdapter` (`shared_services/email/mailchimp_transactional.py`)

Mailchimp Transactional is Mandrill; it is a **separate paid add-on** to a Mailchimp plan and uses its own API key, *not* the Marketing API key (a Marketing key carries a datacenter suffix like `-us14` and will not authenticate here).

- **`send`** — `POST https://mandrillapp.com/api/1.0/messages/send.json` with a `key` field and a `message` object (`from_email`, `from_name`, `to[]`, `subject`, `html`, `text`, optional `headers`, `tags`, `subaccount`, `track_opens`, `track_clicks`, `metadata`). Mandrill authenticates via the JSON body, not a header.
- **From header split:** dispatch passes a combined `"Name <addr@example.com>"`; Mandrill needs the address and display name as separate fields, so the adapter parses them apart (and rejects a header with no `@`).
- **`reply_to`:** Mandrill has no top-level reply-to — it is set as a `Reply-To` message header, merged with any list-unsubscribe `extra_headers`.
- **Credentials secret JSON:** include **`api_key`** (also accepts `MANDRILL_API_KEY` / `MAILCHIMP_TRANSACTIONAL_API_KEY`).
- **`config` (JSON, non-secret):** `subaccount`, `track_opens` (bool), `track_clicks` (bool), `metadata` (object).
- **Tags:** Mandrill reserves tags beginning with `_`; those are dropped, each tag is truncated to 50 chars, and at most 10 are sent.
- **Per-recipient failures are the important difference from other providers.** Mandrill returns **HTTP 200** with an array of per-recipient results whose `status` is one of `sent`, `queued`, `scheduled`, `rejected`, `invalid`. A `rejected` / `invalid` entry is a **failed send inside a success response** — the adapter raises **`MandrillSendRejected`** (carrying `status` and `reject_reason`, e.g. `hard-bounce`, `unsub`, `spam`, `invalid-sender`, `test-mode-limit`, `rule`) so callers get the same failure signal other providers give via HTTP.
- **Retries:** transient `requests.Timeout`, `ConnectionError`, HTTP 5xx and 429 are retried with bounded exponential backoff and jitter. Mandrill returned **HTTP 500 for invalid API keys until Feb 2023** (401 since), so the retry loop matches on the error `name` (`Invalid_Key`, `PaymentRequired`, `ValidationError`, …) and does **not** retry a named permanent error even when it arrives as a 5xx.
- **`message_stream`:** no Mandrill equivalent; the CRM adapter accepts and ignores the argument.
- **Webhooks:** not implemented (same position as Postmark). Mandrill event/inbound ingestion would go in `novaura_crm_backend/communication_processor/views/` alongside the Mailgun webhook path.

### 2.3 Dispatch services (`shared_services/email/email_dispatch.py`)

| Function | Purpose |
|----------|---------|
| **`send_from_contact_endpoint`** | Resolve endpoint’s `email_settings`, load secret, **`get_email_provider_adapter`**, **`adapter.send`**. `from_email` defaults to **`endpoint.value`** (the from address). |
| **`send_hosted_template_from_contact_endpoint`** | Same resolution path; **`adapter.send_template`** (Mailgun stored template + variables). |
| **`send_from_email_config`** | ACS-aware: if **`EmailConfig.email_content_mode == outbound_acs`**, loads approved **`OutboundEmailTemplateVersion`**, runs **`replace_template_variables`** on subject/HTML/text, then **`send_from_contact_endpoint`**. Otherwise **`render_inline_email_body`** (MessageTemplate or raw `content`) then raw send. |
| **`send_via_env_mailgun_fallback`** | No contact endpoint: reads **`MAILGUN_API_KEY`**, **`MAILGUN_DOMAIN`**, optional **`MAILGUN_FROM_EMAIL`** / **`MAILGUN_EU_REGION`** from environment; used by CRM lead email. |

### 2.4 API: send test / operational email

- **`POST /api/communications/send-contact-email/`** (`SendContactEndpointEmailAPIView`)
- Authenticated; body validated by `SendContactEndpointEmailSerializer` (contact endpoint id, to, subject, HTML/text resolution, optional tags, reply-to, from override).
- Returns **`202`** with `message_id`, `message`, **`provider`** (e.g. `mailgun`).

Further frontend-oriented detail: **`docs/FRONTEND_EMAIL_CONTACT_ENDPOINTS_AND_SECRETS.md`**.

---

## 3. ACS app

### 3.1 `EmailConfig` (`acs/models/channel_configs.py`)

- **`email_content_mode`:**
  - **`inline`** — `MessageTemplate` and/or raw **`content`**; merged with context where applicable.
  - **`outbound_acs`** — Body from **`hosted_template_version`** (`OutboundEmailTemplateVersion`); ACS **`TemplateVariable`** rules applied at send time; requires approved version, **`from_endpoint`**, and a subject source (config or version `subject_text`).
- Legacy stored value **`hosted_mailgun`** was migrated toward **`outbound_acs`**; serializers may still normalize the old label.
- Optional **`reply_to`**, **`from_name`** (formatted as `"Name <email>"`), **`track_opens` / `track_clicks`** (stored on config; raw sends today use the adapter’s `send` path—confirm product behavior if tracking must be enforced for outbound_acs).

### 3.2 Outbound email templates (`acs/models/outbound_email_template.py`)

- **`OutboundEmailTemplate`** — slug, name, scoping by account and/or **`LeadNurturingCampaign`**; **`mailgun_template_name`** (stable Mailgun template name after first sync).
- **`OutboundEmailTemplateVersion`** — revisioned HTML/text, variable schema, approval fields, **`mailgun_version_tag`** (e.g. `r{revision}`), **`synced_at`**, **`last_sync_error`**.
- **`OutboundEmailTemplateSyncLog`** — audit of push attempts (request/response summaries, success flag).

### 3.3 Mailgun template sync (`acs/services/mailgun_template_sync.py`)

- **`push_version_to_mailgun(version, contact_endpoint, …)`**
  - Version must be **`approved`**.
  - Contact endpoint must include **email** channel, same **account** (or campaign account) as template, and **`email_settings.provider == mailgun`**.
  - Uses Mailgun **domain template** APIs via `mailgun_push_domain_template_version`.
  - Sets **`mailgun_template_name`** on first push (default pattern `novaura-a{account_id}-{slug}` sanitized) and **`mailgun_version_tag`** to `r{revision}`.

### 3.4 API: template CRUD and sync

| Endpoint | Action |
|----------|--------|
| **`/api/acs/outbound-email-templates/`** | CRUD for template families (`OutboundEmailTemplateViewSet`). |
| **`/api/acs/outbound-email-template-versions/`** | Version CRUD; **`POST …/{id}/approve/`** approves draft/pending_review. |
| **`POST …/outbound-email-template-versions/{id}/sync-mailgun/`** | Body: **`{ "contact_endpoint_id": <id> }`**. Pushes approved version to Mailgun; records errors via **`record_sync_failure`**. |

Supporting services: **`acs/services/outbound_email_variables.py`** (map context to Mailgun `t:variables` when using template sends), **`acs/services/template_variable_render.py`** (merge for outbound_acs raw HTML path).

### 3.5 Bulk nurturing email (`acs/services/bulk_campaign_email.py`)

- **`send_bulk_campaign_email`** resolves effective **`EmailConfig`** on the bulk message funnel, optionally builds **`variable_context`** from lead + campaign when mode is **`outbound_acs`** and no explicit context was passed, then calls **`send_from_email_config`**.
- ACS view: **`POST`** on bulk message **`…/send-email/`** (see `acs/views/lead_nurturing.py`) invokes this path and stores **`provider_message_id`** / metadata (idempotency key support).

---

## 4. CRM lead action (Mailgun without contact endpoint)

- **`POST /api/crm/leads/{id}/actions/send_email/`** — body `subject`, `body` (HTML); **`crm.services.mailgun_client.send_email_via_mailgun`** → **`send_via_env_mailgun_fallback`**.
- Returns **501** with `Mailgun not configured` if env credentials are missing.
- Writes **`LeadActivity`** with provider metadata when successful.

Environment variables (see **`.env.example`** when present in a deployment repo): **`MAILGUN_API_KEY`**, **`MAILGUN_DOMAIN`**, **`MAILGUN_FROM_EMAIL`**, optional **`MAILGUN_EU_REGION`**. Optional defaults for list-unsubscribe when not set on the endpoint: **`LIST_UNSUBSCRIBE_MAILTO`**, **`LIST_UNSUBSCRIBE_HTTPS`**.

---

## 5. Communication processor: Mailgun webhooks

- **URL:** **`POST /api/communication_processor/webhooks/mailgun/email/`**
- Optional query params: **`account_id`**, **`nurturing_campaign_id`** (for attribution on stored rows / SQS payload).
- Resolves **`ContactEndpoint`** by **recipient** address (inbound form or JSON `event-data.recipient`).
- Requires endpoint **`email_settings.provider == mailgun`**; loads credentials; **`verify_webhook_request`**; **`parse_webhook_request`**.
- Persists **`EmailMessage`** (dedupe by `provider` + `provider_message_id`).
- Pushes normalized payload to **SQS** using **`get_email_events_queue_url()`** (requires **`EMAIL_EVENTS_QUEUE_URL`** or configured queue map); returns **500** if queue URL missing.

---

## 6. Outbound send hardening (novaura-acs-processor stack)

Implemented in **`shared_services/email/mailgun.py`**, **`shared_services/message_delivery/message_delivery_service.py`**, **`shared_services/email/email_dispatch.py`**, and **`bulkcampaign_processor/services/bulk_campaign_processor.py`**.

### 6.1 HTTP retries (Mailgun `messages` POST)

- **Transient retries:** `requests.Timeout`, `requests.ConnectionError`, HTTP **5xx**, and **429** (honors **`Retry-After`** when numeric).
- **No retry:** other **4xx** (e.g. 401) — fail immediately.
- **Defaults:** 3 attempts, exponential backoff with jitter (see module constants `MAILGUN_POST_*`).
- **Caveat:** Retrying after a **timeout** can theoretically produce a duplicate send if Mailgun accepted the first request but the client never saw the response. Counts are kept low; bulk **idempotency** (below) covers worker **replay** after success.

### 6.2 Structured logging (`log_context`)

Optional **`log_context`** is passed from **`MessageDeliveryService.send_message`** → **`send_from_email_config`** → **`send_from_contact_endpoint`** → **`MailgunEmailAdapter.send`** → **`send_mailgun_message`**.

Stable log prefixes and fields:

| Event | Logger / prefix | Fields (when provided) |
|-------|------------------|-------------------------|
| Success | `mailgun_send_ok` | `mailgun_message_id`, `contact_endpoint_id`, `nurturing_campaign_id`, `bulk_campaign_message_id`, `send_idempotency_key` |
| Mailgun HTTP failure | `mailgun_send_fail` | `http_status`, `body_preview` (truncated), same ids |
| Success | `postmark_send_ok` | `postmark_message_id`, `contact_endpoint_id`, `nurturing_campaign_id`, `bulk_campaign_message_id`, `send_idempotency_key` |
| Postmark HTTP failure | `postmark_send_fail` | `http_status`, `ErrorCode`, `body_preview` (truncated), same ids |
| Delivery service failure | `email_send_failed` | `error`, same id keys |
| Bulk replay skip | `bulk_email_idempotent_skip` | `bulk_campaign_message_id`, `nurturing_campaign_id`, `send_idempotency_key`, `provider_message_id` |

### 6.3 Bulk email idempotency (`BulkCampaignMessage`)

- **`metadata['send_idempotency_key']`:** Optional client-supplied key; otherwise default **`bulk_campaign_message:{id}`**.
- **On success:** key is merged into **`metadata`** via **`update_status('sent', {'send_idempotency_key': …})`**.
- **Replay guard:** Before send, if **`status == 'sent'`** and **`provider_message_id`** is set, the processor logs **`bulk_email_idempotent_skip`** and returns success without calling Mailgun again (covers aggressive worker retries after Mailgun already accepted the send).
- **Concurrent double-fire:** not fully serialized without a short-lived **`sending`** state or outbox row plus DB migration on **`bulk_campaign_message`** (`managed = False`); coordinate with ops if strict single-flight is required.

### 6.4 EAV merge (lead_field / intake tokens)

After ACS / template merge, **`shared_services/eav_email_merge.py`** substitutes **`{{ lead_field.<api_name> }}`** and **`{{ intake.<api_name> }}`** when `context['lead']` is an ORM **`Lead`** with a campaign.

- **Send:** [`shared_services/email/email_dispatch.py`](shared_services/email/email_dispatch.py) — **`send_from_email_config`** runs **`apply_eav_placeholders_to_email_parts`** for **`outbound_acs`**, **`hosted_mailgun`**, and **inline** paths (including **`merged_html_body`**).
- **Bulk body preview:** [`external_models/models/nurturing_campaigns.py`](external_models/models/nurturing_campaigns.py) — **`BulkCampaignMessage.get_message_content`** wraps merged strings with the same EAV helper so preview matches send.
- **ORM mirrors:** [`external_models/models/lead_eav.py`](external_models/models/lead_eav.py) — unmanaged models aligned with Nova CRM: `lead_field_definition`, `lead_field_value`, `campaign_lead_field_mapping`, `intake_section`, `intake_field`, `lead_intake_value`.
- Do **not** model pure EAV fields as ACS **`TemplateVariable`** rows (avoids conflicting resolution order).

---

## 7. Configuration checklist

1. **Contact endpoint** with `channels` containing **`email`** and **`value`** = authorized From address.
2. **`email_settings`:** `provider: mailgun` with `config.domain`, optional `eu_region`, Secrets Manager ARN with **`api_key`**; or `provider: postmark` with optional `message_stream` / `transactional_stream`, tracking keys, list-unsubscribe keys, and Secrets Manager ARN with **`api_key`** or **`server_token`**. The CRM source-of-truth model and database choices must also allow `postmark`.
3. **Webhooks:** Mailgun route → above URL; secret JSON should include **`webhook_signing_key`** (or supported aliases); app must have **SQS** email events queue configured for enqueue step.
4. **ACS outbound_acs:** Approved **`OutboundEmailTemplateVersion`**, **`EmailConfig`** linked with **`from_endpoint`** and correct mode; optional **sync-mailgun** for domain template copy.
5. **Lead one-off email:** set **`MAILGUN_*`** env vars on the deployment that serves CRM.

---

## 8. Related documentation in this repo

| Document | Topics |
|----------|--------|
| `docs/FRONTEND_EMAIL_CONTACT_ENDPOINTS_AND_SECRETS.md` | Contact endpoints, Mailgun config shape, secrets provision |
| `docs/FRONTEND_OUTBOUND_EMAIL_UI_ALIGNMENT_CHECKLIST.md` | outbound_acs vs Mailgun transport, sync-mailgun UX |
| `docs/FRONTEND_OUTBOUND_EMAIL_TEMPLATES_NEXTJS.md` | Outbound template UI handoff |
| `docs/FRONTEND_OUTBOUND_EMAIL_AND_IMPORT_HANDOFF.md` | Outbound + import flows |
| `novaura_crm_backend/docs/LEAD_DELIVERY_API_ENDPOINTS.md` | CRM `send_email` endpoint |

---

## 9. Key source files (for maintainers)

| Area | Path |
|------|------|
| Mailgun HTTP + adapter | `novaura_crm_backend/communications/email_providers/mailgun.py` |
| Provider registry | `novaura_crm_backend/communications/email_providers/registry.py` |
| Config validation | `novaura_crm_backend/communications/email_providers/config_schema.py` |
| Dispatch | `novaura_crm_backend/communications/services/email_dispatch.py` |
| Send API | `novaura_crm_backend/communications/views/send_contact_email.py` |
| Email settings model | `novaura_crm_backend/communications/models/contact_endpoint_email.py` |
| Webhook | `novaura_crm_backend/communication_processor/views/mailgun_webhook.py` |
| ACS sync | `novaura_crm_backend/acs/services/mailgun_template_sync.py` |
| ACS template APIs | `novaura_crm_backend/acs/views/outbound_email_template.py` |
| EmailConfig | `novaura_crm_backend/acs/models/channel_configs.py` |
| Bulk send | `novaura_crm_backend/acs/services/bulk_campaign_email.py` |
| CRM shim | `novaura_crm_backend/crm/services/mailgun_client.py` |

---

## 10. novaura-acs-processor (native outbound)

Bulk and journey email sends use **`shared_services/email/`** (Mailgun `messages` API + **`shared_services/email/secrets_loader.py`** for Secrets Manager JSON on `ContactEndpointEmailSettings.credentials_secret_arn`). The ECS **task role** must allow `secretsmanager:GetSecretValue` on those ARNs (see `terraform/iam.tf` — `ecs_task_role` already includes this action for runtime tasks).

| Path | Role |
|------|------|
| `shared_services/email/mailgun.py` | Retries, `send_mailgun_message`, structured Mailgun logs |
| `shared_services/email/postmark.py` | Retries, `send_postmark_email`, Postmark JSON payloads, structured Postmark logs |
| `shared_services/email/email_dispatch.py` | `send_from_email_config`, `send_from_contact_endpoint`, `log_context`, EAV after ACS |
| `shared_services/eav_email_merge.py` | `extract_eav_placeholders`, `apply_eav_placeholders`, `apply_eav_placeholders_to_email_parts` |
| `external_models/models/lead_eav.py` | Unmanaged CRM EAV table mirrors |
| `shared_services/message_delivery/message_delivery_service.py` | `send_message` / `_send_email`, `log_context` |
| `bulkcampaign_processor/services/bulk_campaign_processor.py` | Bulk idempotency + `log_context` wiring |
| `external_models/models/nurturing_campaigns.py` | `BulkCampaignMessage.get_message_content` EAV wrapper |

---

*Generated from repository state; behavior should be verified against deployed settings (Secrets Manager, SQS, Mailgun routes, and Django settings).*
