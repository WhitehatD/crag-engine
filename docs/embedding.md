# Embedding the console

The operator console is built to drop into any page as an iframe widget. Every
route works standalone — embed the whole console or a single view (`/console/claims`,
`/console/grounding`, …).

## Quick start

```html
<iframe
  id="crag-console"
  src="https://YOUR-ENGINE-HOST/console/?embed=1"
  style="width: 100%; border: 0; min-height: 480px; background: #0a0a0a"
  title="crag console"
></iframe>
```

`?embed=1` switches the console to chrome-less mode: the header and navigation
are hidden, a minimal "powered by crag" footer badge is shown, and the flag
persists through client-side navigation (open `/console/claims?embed=1` to embed
just the Claims view).

## Height autosize

The console reports its content height to the host page over `postMessage`, so
the iframe never needs an inner scrollbar:

```html
<script>
  window.addEventListener("message", (e) => {
    const msg = e.data;
    if (msg && msg.type === "crag-console:height") {
      document.getElementById("crag-console").style.height = msg.height + "px";
    }
    if (msg && msg.type === "crag-console:ready") {
      // msg.view is the route the console loaded ("/", "/claims", ...)
      console.log("crag console ready:", msg.view);
    }
  });
</script>
```

Messages sent by the console (only when actually framed — it checks
`window.parent !== window`):

| Message | When | Payload |
| --- | --- | --- |
| `{type: "crag-console:ready", view}` | once, on load | `view`: the active route |
| `{type: "crag-console:height", height}` | on load and on every content resize | `height`: document height in px |

## Allowing your site to frame the console

Browsers block cross-origin framing by default. The engine daemon emits a CSP
`frame-ancestors` header on the `/console` mount when you opt in via env:

```bash
# space-separated list of origins allowed to iframe the console
CRAG_ANCHOR_CONSOLE_FRAME_ANCESTORS="https://app.example.com https://docs.example.com"
```

- **Unset (default):** no CSP header is emitted; the browser's same-origin
  default applies (only pages on the engine's own origin can frame it).
- **Set:** responses under `/console` carry
  `Content-Security-Policy: frame-ancestors <your origins>`, so exactly those
  hosts may embed it. The API routes are untouched — the policy is scoped to the
  static console mount only.

## Auth note

The console itself ships no auth — that is an edge concern. If your engine sits
behind a basic-authed private edge (the recommended private deployment), an
iframe on a third-party page cannot supply those credentials; private embedding
works only for hosts inside the same authenticated context. Public embedding is
intended for product surfaces that front their own auth and proxy the engine
(e.g. a cloud dashboard embedding its own tenant's console).

## Deep links

All view state lives in the URL: filters, pagination, selected tab, an open
claim drawer, and the glossary (`?glossary=1`) are all shareable/bookmarkable
and all work inside an embed, e.g.:

```
/console/claims?embed=1&predicate_class=P1&verdict=stale
/console/grounding?embed=1&tab=audit
/console/?embed=1&glossary=1
```
