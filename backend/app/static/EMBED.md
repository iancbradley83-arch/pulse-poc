# Pulse — operator embed integration

How to embed Pulse inside an operator front-end so cards' "Add to Slip" CTA
populates the operator's existing bet slip.

## 1. Embed the widget

```html
<iframe
  src="https://pulse-poc-production.up.railway.app/?embed=1"
  width="420"
  height="780"
  style="border: 0; background: transparent;"
  allow="clipboard-write"
></iframe>
```

`?embed=1` strips Pulse's outer shell chrome so the cards render flush
inside your container.

## 2. Listen for the slip event

When a user taps "Add Bet Builder" / "Tap to bet" on a Pulse card, the
widget posts a structured message to the parent window. Validate the
sender's origin and forward the selections to your slip code.

```js
window.addEventListener('message', (e) => {
  // 1. Validate origin — replace with the URL you embed Pulse from.
  if (e.origin !== 'https://pulse-poc-production.up.railway.app') return;

  const msg = e.data;
  if (!msg || msg.type !== 'pulse:add_to_slip') return;

  // 2. Add to your slip. Apuesta Total / kmianko platforms expose
  //    something like:
  window.addBetSlips(msg.selection_ids);

  // (Optional) Acknowledge so Pulse can animate "added".
  e.source.postMessage(
    { type: 'pulse:slip_ack', card_id: msg.card_id, status: 'added' },
    e.origin
  );
});
```

## 3. Message contract (schema_version: 1)

### Pulse → operator

```jsonc
{
  "type": "pulse:add_to_slip",
  "schema_version": 1,
  "card_id": "f7fe54e1-2e2",         // pulse internal id, opaque
  "bet_type": "bet_builder",          // "single" | "bet_builder" | "combo"
  "legs": [
    {
      "market_label": "Goalscorer",
      "label": "Frenkie de Jong",
      "selection_id": "0QA828718...Q3Q123456",
      "odds": 5.50
    },
    { "market_label": "FT 1X2", "label": "Barcelona", "selection_id": "0ML...", "odds": 1.45 }
  ],
  "selection_ids": ["0QA828718...Q3Q123456", "0ML..."],
  "virtual_selection": "0VS0QA828718...Q3Q123456|0ML...",  // BB only
  "total_odds": 4.13,
  "fixture_id": "828718296361947136",  // rogue event _id
  "hook_type": "team_news",            // injury / team_news / transfer / ...
  "headline": "De Jong returns — Barcelona's engine room fires up",
  "source": "pulse"
}
```

### Operator → Pulse (optional ack)

```jsonc
{
  "type": "pulse:slip_ack",
  "card_id": "f7fe54e1-2e2",
  "status": "added",          // "added" | "rejected"
  "reason": null              // "session_required" | "selection_invalid" | "duplicate"
}
```

## 4. Notes

- **Virtual selections (BBs):** for `bet_type=bet_builder`, prefer
  `virtual_selection` if your platform supports the `0VS<piped>` format
  (Apuesta Total / kmianko / BTI Asia do). Otherwise add each leg from
  `selection_ids` individually.
- **Singles:** `selection_ids` always has exactly one entry.
- **Cross-event combos** (`bet_type=combo`): each leg is on a different
  fixture; `selection_ids` is multi-element and `virtual_selection` is null.
- **Standalone mode:** when Pulse is loaded directly (not iframed), the
  CTA shows a yellow "preview" toast with the JSON payload instead of
  posting. Useful for testing the contract without an embed.
- **Origin:** Pulse posts with `targetOrigin='*'` because the widget
  doesn't know which operator host wraps it. Operators MUST validate
  `e.origin` on their side.
- **No customer auth required from Pulse's side.** The operator handles
  session/auth; Pulse just hands over selection IDs. If your operator
  requires the user to be logged in to populate the slip, surface that
  via the `slip_ack` reason and Pulse will display "Please sign in".

## 5. Versioning

The `schema_version` field will only increment for breaking changes.
Additive fields (new optional keys) won't bump the version. Operators
should ignore unknown keys.
