// Embed + iframe protocol. When the console runs inside a 3rd-party iframe
// (?embed=1) it drops all chrome and speaks a tiny postMessage protocol so the
// host page can autosize the frame.
//
// The embed flag is read ONCE at module load from the initial URL and persisted
// to sessionStorage, so it survives client-side navigation (TanStack Router
// rewrites the URL without a reload and would otherwise lose the query param).

const EMBED_KEY = "crag-console:embed";

function detectEmbed(): boolean {
  try {
    const url = new URL(window.location.href);
    if (url.searchParams.get("embed") === "1") {
      sessionStorage.setItem(EMBED_KEY, "1");
      return true;
    }
    return sessionStorage.getItem(EMBED_KEY) === "1";
  } catch {
    return false;
  }
}

export const IS_EMBED = detectEmbed();

// True only when we are actually framed by a different window.
const IS_FRAMED = (() => {
  try {
    return window.parent && window.parent !== window;
  } catch {
    return true; // cross-origin access threw -> we are framed
  }
})();

function emit(msg: Record<string, unknown>): void {
  if (!IS_FRAMED) return;
  try {
    window.parent.postMessage({ ...msg }, "*");
  } catch {
    /* host gone */
  }
}

export function postReady(view: string): void {
  emit({ type: "crag-console:ready", view });
}

// Report our full document height so the host can size the iframe with no
// inner scrollbar. Debounced via rAF; also fires on ResizeObserver + load.
let _rafPending = false;
function measureAndPost(): void {
  if (_rafPending) return;
  _rafPending = true;
  requestAnimationFrame(() => {
    _rafPending = false;
    const height = Math.max(
      document.documentElement.scrollHeight,
      document.body?.scrollHeight ?? 0,
    );
    emit({ type: "crag-console:height", height });
  });
}

let _installed = false;
export function installHeightReporter(): void {
  if (_installed || !IS_FRAMED || typeof ResizeObserver === "undefined") return;
  _installed = true;
  const ro = new ResizeObserver(() => measureAndPost());
  ro.observe(document.documentElement);
  window.addEventListener("load", measureAndPost);
  measureAndPost();
}
