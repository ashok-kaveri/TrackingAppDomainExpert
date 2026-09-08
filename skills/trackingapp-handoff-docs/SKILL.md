---
name: trackingapp-handoff-docs
description: Use when working inside the TrackingAppDomainExpert project and the user wants professional release handoff documents for the PluginHive Shopify Shipment Tracking & Notifications App - Support Guide, Business Brief, or both - generated from Trello lane cards, approved US/AC, TCs, QA evidence, release metadata, toggles, and member ownership. If the user requests only one document, generate only that document and PDF.
---

# Tracking App Handoff Docs

Use this skill to generate professional handoff documents for approved release cards of the
PluginHive **Shopify Shipment Tracking & Notifications App**.

It produces:

- Combined release Support Guide
- Combined release Business Brief
- Optional single-card Support Guide / Business Brief for quick review
- Markdown + PDF output
- Trello/Slack-ready artifacts when requested

Default release delivery is one combined PDF per document type. If the user asks for only one document
type, generate only that combined document. Use single-card documents only when the user explicitly
asks for one card.

The PDF styling is shared with the MCSL / FedEx / AU Post repos so every PluginHive release package
looks the same. `pipeline/handoff_docs.py` holds the renderer; only `PDF_BRAND` and the domain parts
(app navigation, carrier detection, prompts) differ here.

## First Reads

Before generating:

1. Read `CLAUDE.md` (and `AGENTS.md` if present) for project context.
2. Read `skills/trackingapp-handoff-docs/references/handoff_doc_formats.md`.
3. Inspect only directly relevant project files:
   - `pipeline/handoff_docs.py`
   - `pipeline/trello_client.py`

Use `pipeline/trello_client.py` to fetch the lane, card details, and members. Use
`scripts/send_handoff_pdf_to_slack.py` to deliver PDFs to Slack when explicitly requested.

For release packages, always use full live Trello card context: description, comments, labels,
attachments/checklist summaries, approved AC/TCs, and QA evidence. **Developer and QA comments often
describe what actually shipped, while the card's code-analysis section describes what was proposed —
when they conflict, the comments win.** Late caveats live in comments and must not be skipped.

## Platform

The Tracking App ships on Shopify only. There is no platform audit — write every walkthrough against
Shopify Admin and the app's own sections. `detect_platform_scope()` always reports Shopify.

## Excluded Cards

Before anything else, drop cards that are not part of the release story set. Exclude any card carrying
one of these labels:

- `SL: ON Hold`
- `SL: Carrier Platform`
- `Spill Over`
- `SL: Closed By Support`

Rules:

- Match labels case-insensitively and tolerate emoji, colour prefixes, and extra spacing around the name.
- Exclude the card from the index table as well as the body. A card left out of the body but listed in
  the index reads as a missing section.
- Watch for a lane whose own name is prefixed `ON HOLD — ...`; that lane is not the release lane even
  when the version matches. Confirm the exact lane name before fetching cards.
- Include an excluded card only when the user names it or explicitly asks for it. Naming the card is the
  instruction — do not ask again.
- Never drop a card silently. Always report which cards were excluded and which label triggered it, so a
  short release is visibly deliberate.
- Excluded cards contribute no toggles to the `Toggle List Follow-Up` DM.

Before writing any release package, do a toggle audit for every card:

- Search the whole card for the toggle, not just the description: comments, checklists, attachments,
  approved AC, TCs, and QA evidence. The exact toggle key is often only in a QA or developer comment.
- Put the exact key in the index table `Toggle Name` column. List every key comma-separated when a card
  has more than one, and `None` when the card needs no toggle.
- A merchant-facing setting in General Settings is **not** a toggle. Record it as `None` and describe the
  setting in `Toggles & Prerequisites` instead.
- Never guess or reconstruct a toggle key. If the card clearly needs one but no key is stated anywhere,
  write `Not stated` and flag it in the final response.

Before writing a release Support Guide, do a payload/log audit for every card:

- If the evidence asks support to inspect a carrier field, fulfilment payload, tracking payload, stored
  setting, request log, or diagnostic log, include the exact node/field name support must verify.
- Put the callout immediately after the relevant walkthrough step, using one of these exact bullet labels
  so the PDF renderer highlights it:
  - `Request node to verify: ...`
  - `Request nodes to verify: ...`
  - `Request/response nodes to verify: ...`
  - `Request/log fields to verify: ...`
- Typical Tracking App fields worth a callout: `tracking_company`, `tracking_url`, `carrierFilterMode`,
  `selectedCarrierCodes`, and internal carrier codes such as `C43`.
- Do not add node callouts to UI-only, report-only, or performance cards unless card evidence names an
  actual field or log.
- If the exact field is unknown after checking card comments/checklists and context, say which log to
  inspect and do not invent a field name.

Before writing Support Guide or Business Brief content, do a technical-card audit for every card:

- A technical card is one only a developer cares about: an API-only change, a library or version upgrade,
  a refactor, an internal clean-up, or infrastructure work with nothing support or the merchant can see
  or do.
- Move every technical card into a single `## Technical Cards` section placed after the last normal card
  section. Keep each entry to a few lines: what changed and why it matters.
- Keep technical cards in their normal position in the index table — only the body section moves.
- If a card has both a technical part and something support can see or demo, keep it as a normal card.

## Inputs

Best input package:

- card name/id/url
- release name (for example `SL Tracking App v1.0.27: Iteration backlog`)
- approved US + AC
- reviewed TCs
- QA summary/evidence
- support sign-off notes
- developed by / tested by
- toggles/prerequisites
- known limitations
- rollout notes

If some inputs are missing, still generate a useful draft, but mark unknown fields clearly. Do not invent
ownership, release numbers, toggles, or unsupported limitations.

## Document Selection

Generate based on user request:

- "support guide", "support doc", "demo doc", "customer support explanation" -> combined release Support Guide only
- "business brief", "business doc", "stakeholder doc", "marketing/sales summary" -> combined release Business Brief only
- "handoff docs", "both docs", "support and business" -> both combined release PDFs
- "single card", "only this card", or a specific card id/name -> single-card document for that card

If unclear, ask which one: Support Guide, Business Brief, or both.

## Support Guide Purpose

The Support Guide is for support/demo teams who need to understand the change well enough to explain it
to a merchant.

It must be practical, professional, crisp, and free of technical jargon.

Use no technical words anywhere in the body of either document: no code, class, file, or method names,
no API or schema jargon, and no internal engineering terms. Write it the way you would explain the change
to someone who has never seen the code. Three places are exempt, because the exact string is the point:
the request/log callouts described above, the `Technical Cards` section, and toggle keys in the index
table and `Toggles & Prerequisites` tables.

- Include the Index Page with exactly these columns: "Story ID", "Story Title", "Toggle Name", "Trello card link"
- Explain the feature summary under a heading called "Brief Description". Keep it crisp
- Include where support can see it inside the relevant walkthrough steps
- Explain what the merchant should experience
- Include walkthrough steps
- Include toggles/prerequisites
- Call out any change merchants will notice without asking for it, and any limitation that will generate
  tickets — a support guide that hides a visible behaviour change is worse than no guide

Do not write vague release notes. This should be a real support enablement document.

## Tracking App Navigation

Put the exact area inside the relevant walkthrough step. The app's sections:

- **Orders** — imported order list, `Import Orders` (manual import), per-order shipment detail
- **Carriers** — carrier setup, credentials, carrier exclusions
- **General Settings** — store-wide tracking preferences (for example `Orders to track`)
- **Notifications** — email/SMS templates and trigger rules
- **Tracking Page** — branded tracking portal (logo, colours, banner, timeline)
- **Dashboard / Analytics** — shipment overview, delivery performance, exceptions
- **Plans page** — subscription, order allowance, trial
- **Shopify Admin > Orders** — cross-check fulfilment, carrier name, and tracking data at source

Reach the app itself via **Shopify Admin > Apps > PluginHive Shipment Tracking & Notifications**.

## PDF Generation

When the user asks for PDF:

1. Generate the markdown first.
2. Save the markdown under `data/handoff_docs/`.
3. Render PDF using:
   `skills/trackingapp-handoff-docs/scripts/render_handoff_pdf.py`

```bash
PYTHONPATH=. .venv/bin/python skills/trackingapp-handoff-docs/scripts/render_handoff_pdf.py \
  --markdown data/handoff_docs/<name>.md \
  --title "<doc title>" \
  --out data/handoff_docs/<name>.pdf
```

For one requested release document, create one combined PDF containing all selected/approved release
cards. For both, create two combined PDFs: one Support Guide package and one Business Brief package.

After rendering, check the PDF for layout faults before delivering: the renderer inserts a page break
before every card section, so content that ends exactly at a page boundary can leave a blank page. If a
page comes out blank, tighten the preceding section rather than hand-placing breaks.

## Slack Delivery

Send with `scripts/send_handoff_pdf_to_slack.py`. Nothing is sent without `--yes`, so always dry-run
first and show the resolved target.

```bash
# dry run — prints target, filename, size, message text
PYTHONPATH=. .venv/bin/python scripts/send_handoff_pdf_to_slack.py --pdf <pdf path> --title "<doc title>"

# DM to the doc owner (default target)
PYTHONPATH=. .venv/bin/python scripts/send_handoff_pdf_to_slack.py --pdf <pdf path> --title "<doc title>" --yes

# team channel: bare --channel targets qa_members_internal
PYTHONPATH=. .venv/bin/python scripts/send_handoff_pdf_to_slack.py --pdf <pdf path> --title "<doc title>" --channel --yes
```

**When the request already names a destination, that is the approval — do not ask again.**
"DM me the PDF", "send it to me", "share it in #qa-team" all authorise that one send: dry-run,
then send in the same turn, then report the target and file id. Re-sending a corrected version
of a document the user already asked to be sent needs no fresh approval either.

Ask first only when:

- the user asked for a document but named no destination, or
- the send target differs from the one they named — in particular, approval for a DM is never
  approval for a team channel, and vice versa.

Always report the outcome: target, file id on success, or the exact Slack error on failure.

## Toggle List Follow-Up

After a release package is generated, send the consolidated toggle list as a Slack DM to
`ashok@pluginhive.com`. This is a standing instruction from the doc owner, so it needs no fresh
approval — but always show the message text before sending, then report the result.

Rules:

- Send the toggle list only. Never attach the document to this message; document delivery stays under
  `Slack Delivery` above.
- Skip the step entirely when no card in the release has a toggle. Say so in the final response instead
  of sending an empty message.
- Build the list from the `Toggle Name` column of the guide's index table. One line per toggle, in this
  exact shape:

```
"<account uuid>.<toggle name>": true,
```

- **This repo does not store the Tracking App account UUID.** Never invent one, and never reuse another
  app's UUID. Take it from the release owner or the card evidence; if it is not available, send the
  toggle names with a placeholder and say plainly that the UUID needs filling in.
- Never invent a toggle, never repair a malformed UUID, and list anything left out with a one-line reason.

## Combined Release Package Structure

Combined Support Guide:

```markdown
# <Release> Support Guide

## Included Story Cards
| Story ID | Story Title | Toggle Name | Trello card link |
|---|---|---|---|

## <Story ID> - <Card title>
### Brief Description
...

## Technical Cards
### <Story ID> - <Card title>
...
```

Do not add a `How Support Should Use This Package` section. The index page is followed directly by the
first card section.

Combined Business Brief:

```markdown
# What's New: <Release>

## Release Overview
...

## Included Updates
| Story ID | Story Title | Toggle Name | Trello card link |
|---|---|---|---|

## <Story ID> - <Card title>
### Brief Description
...

## Technical Cards
### <Story ID> - <Card title>
...
```

## Technical Cards Section

Layout rules, which follow how `render_pdf_bytes` breaks pages:

- Use one H2 `## Technical Cards` after the last normal card section. Inside a combined package the
  renderer page-breaks before every non-package H2, so this section gets its own page automatically —
  never hand-place a break.
- List each technical card under it as an H3 `### <Story ID> - <Card title>` so the short entries flow
  together instead of taking a page each.
- Two to four lines per card: what changed, and why it matters for the product or the merchant. No
  walkthrough, no toggles section, no expected-behaviour section.
- Plain wording still applies. Name a version, endpoint, or field only when the entry makes no sense
  without it.
- Omit the section entirely when the release has no technical cards.

## Support Guide Structure

For each card section inside the combined Support Guide:

```markdown
# Support Guide: <Story ID or concise feature name>

## Brief Description
...

## Toggles & Prerequisites
...

## Step-by-Step Support Walkthrough
...

## Expected Behaviour
...
```

Do not add `Merchant-Safe Explanation`, `Common Questions & Troubleshooting`, or
`Support Escalation Packet` sections. The card section ends after `Expected Behaviour`.

## Quality Bar

Before finalizing:

- make it understandable for support people
- remove internal/code jargon entirely from the body; the only exceptions are request/log callouts and
  the `Technical Cards` section
- verify every card's toggle was searched for across comments, checklists, and QA evidence, not just the
  description
- verify no card labelled `SL: ON Hold`, `SL: Carrier Platform`, `Spill Over`, or `SL: Closed By Support`
  slipped into the index table or the body
- verify technical-only cards sit in the `Technical Cards` section at the end, not mixed into the
  walkthrough cards
- verify the guide describes what shipped per the card comments, not the proposed fix from the card's
  code-analysis section
- keep merchant-facing wording safe and clear
- do not expose implementation details that customers do not need
- verify every claim comes from card/AC/TC/QA evidence or researched domain facts
- verify every live Trello QA comment and checklist has been considered before finalizing a release package
- include exact app-aware navigation in the relevant walkthrough step instead of a generic location section
- include highlighted exact field names when the card requires payload, carrier-field, stored-setting, or
  diagnostic-log verification
- every story card starts on a new page, including the first — the index page stands alone and the
  renderer inserts the breaks, so never hand-place one
- verify no card heading starts at the bottom of a page without its detail table/content following, and
  no page renders blank
- keep the support guide thorough enough for a support call
- keep the business brief short and polished

## Final Response

Return:

- document(s) generated
- markdown path if saved
- PDF path if rendered
- whether the toggle list DM was sent, skipped because no card has a toggle, or failed
- which cards were excluded and the label that triggered each exclusion
- any missing inputs or assumptions

Use absolute file paths in final responses.
