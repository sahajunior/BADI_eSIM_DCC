# ADR 0006: Attachments and reply notifications via durable records

- **Status:** Implemented in Phase 6 (migrations `0006_attachments`, `0007_notifications`).
- **Context:** Attachments and reply emails are optional bonuses, but they touch three
  risk areas: private content leaking to the wrong viewer, blocking a saved reply on an
  external provider, and duplicate side effects on retry.
- **Decision:**
  - **Attachments:** Store metadata in `attachments` with a random private storage key,
    detected media type, checksum, and scan state. Validate by content signature, not by
    the browser `Content-Type`; fail closed when scanning is unavailable. A file attaches
    only through an authorized message on the same ticket, by its uploader, once, while
    clean. Download re-checks the parent message visibility; staged uploads are
    uploader-only. `python -m app.cleanup` removes expired unattached objects.
  - **Notifications:** Write one `notification_deliveries` row per logical
    message/recipient/channel **inside the reply transaction**; a worker delivers with
    retry/backoff and dead-letters failures. Never notify the author; unassigned-ticket
    customer replies record a queue notification instead of emailing every agent;
    INTERNAL messages never enter the customer email path. Email content is generic
    (ticket number + login link), never the body or attachments.
- **Rejected:** Proxying/storing objects in the database; trusting upload headers;
  sending notifications inline in the request (provider outage would fail the reply);
  embedding message bodies in email; notifying all staff for unassigned tickets.
- **Consequences:** A mail or scanner outage degrades a bonus, not the core reply. Unique
  delivery keys make replay safe. Private storage is a local adapter placeholder; a real
  deployment swaps it for private object storage with the same authorization boundary.
- **Verification:** `tests/integration/test_attachments_phase6.py` and
  `test_notifications_phase6.py` (cross-user/internal denial, spoofed/oversized files,
  scanner outage, cross-ticket/double bind, orphan cleanup; routing matrix, author
  suppression, preferences, provider outage/dead-letter, content safety, replay).
