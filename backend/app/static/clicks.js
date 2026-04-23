/* =====================================================================
 *  PULSE — CTA click tracking (Stage 5 deep-link analytics)
 *
 *  Fires a POST /api/cards/{id}/click the moment the user taps a card's
 *  "Tap to bet" / "Add Bet Builder" CTA — BEFORE window.open() takes them
 *  to the operator's slip. Uses navigator.sendBeacon so the request
 *  doesn't race the tab switch (browsers guarantee beacon delivery for
 *  in-flight requests when the page unloads).
 *
 *  Falls back to fetch+keepalive when sendBeacon is unavailable
 *  (older Safari, private mode). Both paths are fire-and-forget — we
 *  never block the deep-link on the analytics POST succeeding.
 *
 *  Kill switch: if the server returns 404 (PULSE_REACTIONS_ENABLED=false
 *  also disables click tracking — one flag, one analytics surface),
 *  subsequent calls short-circuit so we stop hammering a disabled endpoint.
 * ===================================================================== */

window.PulseClicks = (() => {
  let disabled = false;

  function track(cardId) {
    if (disabled || !cardId) return;
    const url = `/api/cards/${encodeURIComponent(cardId)}/click`;

    // Prefer sendBeacon — it's the only transport with a hard guarantee
    // against being cancelled by the imminent tab switch. It's POST-only,
    // no headers config, so we pass an empty Blob with JSON MIME to keep
    // the server from treating it as form-urlencoded.
    try {
      if (navigator.sendBeacon) {
        const blob = new Blob([JSON.stringify({ card_id: cardId })], {
          type: 'application/json',
        });
        const ok = navigator.sendBeacon(url, blob);
        if (ok) return;
        // Some UAs return false when the request is too large or CSP
        // blocked — fall through to the fetch path.
      }
    } catch (e) { /* fall through */ }

    // Fetch fallback with keepalive so the request survives a nav.
    try {
      fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ card_id: cardId }),
        keepalive: true,
      }).then(res => {
        if (res && res.status === 404) disabled = true;
      }).catch(() => { /* ignore — click already fired visually */ });
    } catch (e) { /* ignore */ }
  }

  return { track };
})();
