# Images in Support Answers — Odoo side (Cloudunify)

> **Handoff note, written 2026-08-12.** The REVA half is implemented, tested and
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
| **Odoo extracts and sends the images** | ❌ **this document** |
| Contracts copied into `Cloudunify/reva_contracts/` | ❌ do this first |

## Step 0 — sync the contract

`contracts/inbound/support-request.{schema,sample}.json` and
`contracts/manifest.json` in this repo are regenerated (contracts_version
`3421e338…`) and include `images` + the `ImageAttachment` definition. Copy them
into `Cloudunify/reva_contracts/inbound/` and bump whatever hash pin the
Cloudunify contract test uses. **Cloudunify only.**

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

- Odoo-side unit tests: marker/array alignment; a dropped image leaves no
  dangling `[Image N]`; a signature logo is filtered; a malformed `src` logs
  instead of raising; labels come out as `Image 1..N` with no gaps.
- **End-to-end, and this is the real test:** re-send ticket 6891 with its two
  screenshots. REVA should name `[200028] IBC Container 1000l mit Glykol pur`
  and stop asking which product is affected. Until that runs, the feature is
  unit-tested only.

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
