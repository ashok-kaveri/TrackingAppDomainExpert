# SL Tracking App v1.0.27 — Support Guide

Release: Shopify Shipment Tracking & Notifications App v1.0.27. Platform covered: Shopify. Three story cards are included below. Confirm the merchant is on the v1.0.27 build before promising any behaviour described here.

All three cards trace back to a single merchant escalation. The merchant's Amazon orders were not being imported at all, while orders for a carrier they had never set up were being imported and eating into their order allowance. Two cards fix those two halves — the app now recognises carrier names it previously did not, and it now only imports orders for carriers the store is actually allowed to track. The third card fixes a long-standing complaint about the manual import: the Import Orders window used to stay open for minutes on large stores, and now closes as soon as the import has been handed off to run in the background.

One change in this release is visible to merchants without them asking for it. Stores that were importing orders for a carrier they never set up will see fewer orders tracked, and lower usage against their plan, after upgrading. This is the intended fix, but it needs to be explained rather than discovered — the detail is under TRX-026.

## Included Story Cards

| Story ID | Story Title | Toggle Name | Trello card link |
|---|---|---|---|
| TRX-025 | Amazon orders fail import due to non-standard tracking_company value [#403300] | None | [TRX-025](https://trello.com/c/r9qpVH6g) |
| TRX-026 | Scope subscription order-import limit to Amazon Shipping only [#403300] | None | [TRX-026](https://trello.com/c/OGkHbCNO) |
| TRX-027 | Fire-and-Forget Manual Order Import | None | [TRX-027](https://trello.com/c/xIipcM8b) |

## TRX-025 - Amazon orders fail import due to non-standard tracking_company value [#403300]

### Brief Description

When a Shopify order was fulfilled with Amazon as the carrier, the app sometimes did not import the order at all. Nothing failed visibly — the merchant simply saw "No new orders to import" and had no way to tell that a shipment had been skipped.

The cause was the carrier name Shopify sends with the fulfilment. The app matched that name against a fixed list of exact spellings. It knew "Amazon Shipping", "Amazon Logistics UK" and "Amazon Logistics US", but a store sending the plain name "Amazon" matched none of them, so the shipment was quietly dropped. The app now recognises carrier names far more loosely, and can also work out the carrier from the tracking link when the name alone is not enough.

### Toggles & Prerequisites

| Item | Detail |
|---|---|
| Platform | Shopify |
| Toggle | None |
| Version | The merchant must be on the v1.0.27 build |
| Scope | Applies to every supported carrier, not just Amazon |
| Coverage | Both routes orders arrive by — the manual import of past orders, and live fulfilment updates coming from Shopify |
| Setup | The carrier must also be one the store is allowed to track. If a shipment still does not import after this fix, check the carrier selection covered under TRX-026 before escalating |

### Step-by-Step Support Walkthrough

**Scenario A — a carrier name the app previously did not recognise**

1. In **Shopify Admin → Orders**, pick an order fulfilled with a carrier name that is not the app's standard spelling — the plain name "Amazon" is the reported case.
2. Open that fulfilment in Shopify and note exactly what carrier name and tracking link are stored against it. This is the value the app has to interpret, and it is the first thing to capture on any "my orders are not importing" ticket.
- Request/log fields to verify: `tracking_company`, `tracking_url`
3. In the app, open **Orders** and use **Import Orders**, choosing a period that covers that order.
4. Confirm the order now appears in the app's order list. Before v1.0.27 this order would have been skipped with no message beyond "No new orders to import".
5. Open the imported order and confirm the carrier is shown as Amazon Shipping.
6. Confirm tracking events are being fetched for that order, so the shipment is genuinely being followed and not just listed.

**Scenario B — everyday variations of a carrier name**

7. Fulfil test orders using variations of a carrier name the app already knew: different capitalisation, extra or missing spaces, added punctuation, a trademark symbol. Confirm each one resolves to the correct carrier.
8. Fulfil test orders using regional or service variations such as Amazon India, FedEx India or UPS Express India. Confirm each resolves to its parent carrier.
9. Fulfil a test order with an unapproved word added to a known carrier name. Confirm the app does **not** force a match — it is designed to make no match rather than a wrong one.

**Scenario C — carrier identified from the tracking link**

10. Fulfil a test order whose carrier name is unhelpful but whose tracking link points at a genuine carrier's tracking site. Confirm the app identifies the carrier from the link.
11. Fulfil a test order with a lookalike or fake tracking domain. Confirm no carrier is assigned from that link — the app deliberately rejects domains it cannot trust.
12. Repeat steps 10 and 11 on the live route: fulfil an order in Shopify while the app is connected so the update arrives automatically, rather than using manual import. Behaviour must be identical on both routes.

**Scenario D — regression check**

13. Fulfil an order using Shopify's "Other" carrier option with a recognisable tracking number. Confirm it still resolves the way it did before the release — this path was deliberately left unchanged.

### Expected Behaviour

- Orders fulfilled with a non-standard carrier name now import and are tagged with the correct carrier.
- Differences in capitalisation, spacing, punctuation and trademark symbols no longer stop a carrier from being recognised.
- Regional and service variations of a carrier name resolve to the parent carrier.
- Where the name is unusable, the tracking link is used instead, and untrustworthy or shared tracking domains are rejected rather than guessed at.
- Where a name could belong to more than one carrier, no carrier is assigned. A missing carrier is treated as safer than a wrong one.
- **Known limitation, and the one to keep in mind on tickets:** a carrier name the app genuinely cannot place is still skipped silently, with nothing shown to the merchant beyond "No new orders to import". Adding a visible signal for skipped shipments was raised as a follow-up and is not in this release. A future ticket of this kind will look exactly like this one did, so capture the stored carrier name and tracking link early when triaging.

## TRX-026 - Scope subscription order-import limit to Amazon Shipping only [#403300]

### Brief Description

The app used to import an order for any carrier it recognised, whether or not the merchant had actually set that carrier up. A merchant on an Amazon-only plan found their FedEx orders being imported and counted against their order allowance, even though FedEx was never set up on their store — so the orders they did want were being crowded out of their own plan.

The app now only imports orders for carriers the store is allowed to track, and a new **Orders to track** setting lets the merchant narrow that down further to a chosen list. Because the plan allowance only ever counts orders that were actually imported, skipping unwanted orders also stops them consuming the allowance.

### Toggles & Prerequisites

| Item | Detail |
|---|---|
| Platform | Shopify |
| Toggle | None — this is controlled by a merchant-facing setting, not a backend switch |
| Version | The merchant must be on the v1.0.27 build |
| Where | The app's **General Settings → Orders to track** |
| Default | Existing stores keep their current behaviour. The setting starts on "all supported carriers", so nothing changes until the merchant narrows it |
| Which carriers count as allowed | Carriers the merchant has set up with credentials, plus carriers that work without credentials, minus any carrier the merchant has switched off |
| Validation | At least one carrier must be chosen when "Selected carriers only" is used |
| Rollout note | A store that was importing orders for a carrier it never set up will now stop importing them. Their tracked-order count and plan usage will drop compared with previous months. This is the fix working, but it is visible to the merchant and should be explained up front |

### Step-by-Step Support Walkthrough

**Scenario A — a carrier that was never set up is no longer imported or billed**

1. Use a store with Amazon Shipping set up and FedEx never set up. Note the current plan usage figure before you start.
2. Make sure the store has both Amazon and FedEx fulfilments inside the period you are about to import.
3. In the app, open **Orders** and run **Import Orders** for that period.
4. Confirm the Amazon orders are imported and the FedEx orders are not.
5. Check the plan usage figure again. It must have risen only by the Amazon orders. The FedEx orders must not have consumed any of the allowance — this is the specific complaint the card was raised for.
6. Now fulfil a FedEx order in Shopify so the update reaches the app on the live route rather than by manual import. Confirm no order is created and the usage figure does not move.

**Scenario B — the Orders to track setting**

7. Open the app's **General Settings** and find the **Orders to track** section. On a store that has just been upgraded, confirm it is set to all supported carriers and that import behaviour is unchanged.
8. Switch it to "Selected carriers only" and try to save without choosing a carrier. Confirm the save is blocked and the app asks for at least one carrier.
9. Choose Amazon Shipping only and save. Confirm the choice sticks after a reload. If a merchant reports orders going missing after an upgrade, this saved selection is the first thing to check.
- Request/log fields to verify: `carrierFilterMode`, `selectedCarrierCodes` (Amazon Shipping is stored as `C43`)
10. Run an import over a period containing both Amazon and non-Amazon orders. Confirm only the Amazon orders are imported and counted.
11. Switch a carrier off using the existing carrier-exclusion controls, then run an import. Confirm orders for that carrier are skipped at import time. Before this release an excluded carrier was only filtered out later, during the daily tracking refresh, so the orders were still imported and still counted.
12. Change a date preference and a carrier preference in the same save. Confirm both are stored together and there is no half-saved state.

**Scenario C — an order containing more than one carrier**

13. Fulfil a single order with two shipments, one on a selected carrier and one on a carrier that is not selected.
14. Import it and confirm the shipment on the selected carrier is imported while the other is ignored. The whole order must not be rejected because one shipment was not wanted.

### Expected Behaviour

- Orders for carriers the store has not set up, or has switched off, are no longer imported.
- Those orders no longer count against the merchant's plan allowance. The count and the import now follow the same rule, so the two can no longer disagree.
- Merchants can narrow tracking to a chosen list of carriers under **Orders to track**, and cannot save an empty list.
- Orders holding shipments from several carriers are imported in part — the wanted shipments come in, the rest are ignored.
- Carrier exclusions now apply at the moment of import, not only during the daily tracking refresh.
- Stores upgrading with no changes to their settings see no difference in behaviour.
- **Expect this ticket:** "fewer orders are being tracked since the update". Check the store's Orders to track selection and its switched-off carriers first. If the merchant does want those orders, the answer is to set the carrier up, widen the selection, or switch the carrier back on — not to look for a fault.
- **Open point:** whether a merchant can switch off a carrier they have actively set up is being followed up separately. For now, point them at "Selected carriers only" instead.

## TRX-027 - Fire-and-Forget Manual Order Import

### Brief Description

The **Import Orders** window used to stay open until the entire import had finished. On a store with a large order history that meant several minutes of a spinning window, and merchants reasonably assumed the app had frozen.

The app now starts the import, confirms that it has started, and closes the window straight away. The import carries on in the background. A **Refresh** button was added to the order list so merchants can pull in orders as they arrive, and the app now checks the store's remaining order allowance before starting rather than partway through.

### Toggles & Prerequisites

| Item | Detail |
|---|---|
| Platform | Shopify |
| Toggle | None |
| Version | The merchant must be on the v1.0.27 build |
| Scope | The manual **Import Orders** action only. Live fulfilment updates from Shopify are unaffected |
| Setup | For a realistic test, use a store with a large order history — the old behaviour was only obvious at scale |
| Not included | The speed of the import itself is unchanged. Only the waiting behaviour changed. Making the import faster is tracked separately as TRX-028 |

### Step-by-Step Support Walkthrough

1. On a store with a large order history, open the app's **Orders** page and choose **Import Orders**.
2. Select the longest period available and start the import.
3. Confirm the window closes promptly and tells you the import has started. It must not sit spinning while the import runs.
4. Wait a few moments, then use the new **Refresh** button on the order list. Confirm newly imported orders appear without reloading the whole page.
5. Keep refreshing every so often and confirm the order count keeps climbing until the import finishes. This is what proves the import is genuinely still running after the window closed.
6. Close the browser tab while the import is still running, then come back to the app later. Confirm the remaining orders still arrived — closing the tab does not stop the import.
7. On a store that has already used up its order allowance, start an import. Confirm it is stopped up front with an allowance message rather than running and then failing.
8. Run a small import of a handful of orders and confirm exactly the same orders are imported as before the release. Only the timing of the confirmation changed, not the outcome.

### Expected Behaviour

- The Import Orders window closes within seconds no matter how large the store is.
- The message means the import has **started**, not finished. Large imports still take minutes to complete, and the order list fills up gradually.
- The **Refresh** button brings in newly imported orders without a full page reload.
- Closing the browser or navigating away does not interrupt an import that is already running.
- A store at its order allowance is stopped before the import begins.
- The set of orders imported is identical to before. Nothing about which orders are picked up changed in this card.
- **Important for support:** because the window now closes immediately, an import that fails part way through looks exactly the same to the merchant as one that succeeded. Failures are only recorded in the app's import log. Never confirm to a merchant that an import completed on the strength of the confirmation message alone — check the import log, or check that the expected orders actually landed.
