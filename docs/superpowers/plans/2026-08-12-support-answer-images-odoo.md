# Images in Support Answers — Odoo side (Cloudunify)

> **DONE 2026-08-26 — implemented, not committed.** Both halves are in place.
> The Odoo work is in `../Cloudunify` on the `Prod` branch as **uncommitted
> working-tree changes**, awaiting Joseph's review and commit; the REVA side is
> committed here. The end-to-end check (re-send ticket 6891) is still owed —
> see *Verification* below.
>
> *Original handoff note, 2026-08-12:* the REVA half is implemented, tested and
> deployed. This is the remaining half, and it is what actually makes the
> feature do anything: **REVA is inert until Odoo starts sending images.**
> Implement in `../Cloudunify` (`custom_addons/cu_reva_ticket_analysis`).
> **ast-odoo is retired — do not open a PR there**, even though it still carries
> a stale copy of the same addon.

**Spec:** `docs/superpowers/specs/2026-08-10-support-answer-images-design.md`
**REVA-side plan (done):** `docs/superpowers/plans/2026-08-10-support-answer-images.md`

## Where things stand

| Piece | State |
|---|---|
| `images` accepted on `POST /api/v1/support-request` | ✅ deployed |
| Messages-API image content blocks | ✅ deployed |
| CLI-escalation image staging (`--add-dir` + `Read`) | ✅ deployed |
| `support_turns.image_count` + requeue ops event + TUI column | ✅ deployed |
| Skill prompt tells the model to read images | ✅ deployed (prompts v2.20) |
| **Odoo extracts and sends the images** | ✅ 2026-08-26 (`models/reva_images.py`, uncommitted in Cloudunify) |
| **Ticket analysis takes images too** | ✅ 2026-08-26 — see the addendum below |
| Contracts copied into `Cloudunify/reva_contracts/` | ✅ re-synced 2026-08-26 (`a70c331492c2…`, supersedes `8f7c2d31d57f…`) |

## Step 0 — sync the contract — ✅ DONE 2026-08-20

`Cloudunify/reva_contracts/` is synced to contracts_version `8f7c2d31d57f…`
(which supersedes the `3421e338…` this plan was written against) and the pin in
`cu_reva_connector/tests/test_contracts.py` is bumped. `images` and the
`ImageAttachment` definition are in place on the Odoo side; the array is
optional and top-level `required` is unchanged, so nothing there sends them yet.
Start at "The contract you are filling in".

## The contract you are filling in

```jsonc
"images": [
  {"filename": "shot.png", "label": "Image 1", "content_base64": "iVBORw0…"}
]
```

- Accepted: `.png` `.jpg` `.jpeg` `.gif` `.webp`. Extension is the authoritative
  gate and REVA verifies the bytes against it — a `.png` renamed `.jpg` is a 422.
- `label` **must** match `^Image \d{1,2}$` exactly. It is pinned because it is a
  text block sitting outside REVA's nonce fence, immediately ahead of untrusted
  image bytes. `"Bild 1"`, `"image 1"`, or anything with punctuation is a 422.
- Caps: **6** images, **5 MB** each decoded, **8 MB** total decoded. Over any of
  them is a 422 that names the offending image.
- Omitting `images` entirely is still valid — that is today's behaviour.

## The work

`custom_addons/cu_reva_ticket_analysis/models/reva_mixin.py:895` currently does:

```python
question = html2plaintext(getattr(self, "description", "") or "")
```

That single call is the bug. It renders every `<img>` as a bare `Image [N]`
marker plus a footnote list of `/web/image/…` URLs, which is exactly what REVA
received on ticket 6891 — placeholders pointing at pictures it could not fetch.
Replace it with an extract-then-flatten pass:

1. **Walk the description HTML** for `<img>` in document order.
2. **Resolve each to bytes.** Handle `src="/web/image/<id>"` (with or without
   `?access_token=`) → `ir.attachment` by id, and inline `data:image/…;base64,…`.
   Anything else — `/web/image/<model>/<id>/<field>`, external URLs — is dropped
   with a log line. **Do not fetch external URLs out of customer mail.**
3. **Filter.** Drop when: mimetype outside the five; decoded size > 5 MB; long
   edge < 250 px; or the 6-image / 8 MB budget is spent. Keep the *first*
   survivors in document order — in a reply-style mail the screenshots come
   before the signature, which is why images [3] and [4] on 6891 were noise.
4. **Downscale** with Pillow when the long edge exceeds 2576 px. Keep PNG as PNG:
   both configured REVA models are high-resolution tier (2576 px, ≤4784 visual
   tokens), and heavy JPEG recompression is exactly what makes small table text
   unreadable. Cost is not a reason to trade fidelity away — a 1920×1080
   screenshot is ~2691 tokens ≈ $0.008.
5. **Rewrite the DOM before flattening.** Replace each *kept* `<img>` with a text
   node `[Image N]` and *remove* each dropped one, then run `html2plaintext`.
   This is the load-bearing step: it keeps the markers in `question` in lockstep
   with the `images` array and leaves no dangling marker for an image REVA never
   received. It also suppresses html2plaintext's own footnote numbering, which
   counts every link — not just images — and therefore cannot be used as an
   image index.

Apply the same treatment to `_reva_submit_analysis` (`reva_mixin.py:804`), which
flattens the same `description` the same way and is equally blind today.

> **Resolved 2026-08-26.** This line could not be followed as written when the
> plan was drafted: `/api/v1/ticket-analysis` had no `images` field — only the
> support half of the spec shipped in the first pass, despite the spec's title
> covering both. Rather than settle for marker-only cleanup on that path, the
> REVA side was extended to match (Joseph's call). Both submit methods now go
> through one mixin helper, `_reva_description_for_reva`, so the markers and the
> array can never disagree between them. `action_create_github_issues` still
> flattens plainly — that contract has no `images` field and is out of scope.

**Failure posture:** matches the existing sender — a broken image is skip-and-log,
never a failed submit. The button must not start refusing to send because one
`<img>` had an odd `src`.

## The signature-logo problem is the hard part

On 6891 the mail carried four images: two screenshots (the real question) and two
signature logos. A naive "send everything" implementation burns budget on logos
and dilutes the prompt. The 250 px long-edge rule is the cheap first cut;
document order plus the 6-image cap is the second. If AST's logo survives both,
consider also dropping images that appear after the first `<hr>` / signature
separator, or that repeat byte-identically across tickets.

## Verification

- Odoo-side unit tests: **done** — `tests/test_reva_images.py`, 29 tests across
  three classes (marker/array alignment, no dangling marker, no gaps in the
  numbering, signature-logo drop, the caps, the resize, both `src` shapes, the
  attachment-authorisation matrix, and both submit paths). Full addon suite
  **811 passed / 0 failed** against a live Odoo 19 registry.
- **Still owed — end-to-end, and this is the real test:** re-send ticket 6891
  with its two screenshots. REVA should name `[200028] IBC Container 1000l mit
  Glykol pur` and stop asking which product is affected. Until that runs, the
  feature is unit-tested only: every test here mocks the HTTP call, so green
  proves the payload shape, not that the model reads a screenshot.

## Known REVA-side limitation to keep in mind

Requeue (`POST /api/v1/support-turn/{turn_id}/requeue`) rebuilds params from the
DB and therefore drops images — same as it already drops `chatter` and
`attachment`. It records a `requeue_lost_images` ops event and the TUI shows an
Img column, so the loss is visible, but **the operator fix is to press the Odoo
button again**, not to requeue. Worth a line in the addon's user-facing help.

## Out of scope

- Images on `comment_reply`, `timesheet_review`, or audit paths.
- Files API upload + `file_id` reuse (only worth it if multi-turn image threads
  become common; REVA replays prior turns as text summaries today).
- Lossless requeue via a `support_turn_images` blob table.


## Addendum 2026-08-26 — ticket analysis, and what "not stored" means

**Ticket analysis was brought up to the support path.** `POST
/api/v1/ticket-analysis` now takes the same `images` array, gated by the same
accept-time check (extracted to `api/app/image_gate.py` — a cap that drifts
between two endpoints is a cap that does not exist). `TicketAnalyzer` sends the
image content blocks behind the same untrusted-data preamble (`IMAGES_PREAMBLE`,
now shared from `reva/image_attachment.py`), and the planner-gated CLI
escalation stages them as files through `worker/worker/image_staging.py`, shared
with `support_runner`. Prompts v2.21 tells both the Messages-API prompt and the
`reva-ticket-analysis` skill to read them and never raise a `missing_info`
question a screenshot already answers.

`ticket_analyses.image_count` (migration 048) mirrors `support_turns.image_count`,
and `requeue_ticket_analysis` emits `requeue_lost_images` for the same reason:
the requeue rebuilds params from the row, so it re-analyses blind.

**Nothing stores the bytes, by design** (Joseph, 2026-08-26 — space):

- **Odoo** creates no attachments. The screenshots already exist as the mail
  gateway's inline `ir.attachment` records; they are read, resized in memory,
  base64'd into the request, and dropped.
- **`reva.request.log`** already strips them: `summarize_payload`'s
  `_BASE64_KEYS` contains `content_base64` and it recurses into lists, so each
  image logs as `{"filename": …, "bytes": N}`. That was incidental before and is
  load-bearing now (8 MB per send into a permanent table), so
  `test_request_log_records_the_size_not_the_bytes` pins it.
- **REVA** persists a count and nothing else. The bytes live in the RQ payload
  in Redis for the job's lifetime and in the Anthropic request. The deferred
  `support_turn_images` blob table stays deferred.
