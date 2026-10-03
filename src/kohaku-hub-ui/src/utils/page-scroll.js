const RESTORE_TIMEOUT = 5000;
const SCROLL_KEYS = new Set([
  "ArrowUp",
  "ArrowDown",
  "PageUp",
  "PageDown",
  "Home",
  "End",
  " ",
]);

/** Vue Router's savedPosition belongs to window, not the page's scroll area. */
export function createPageScrollBehavior() {
  const positions = new Map();
  let currentEntry;
  let traversal = false;
  let pending;
  let cancelRestore = () => {};
  let removeBefore;
  let removeAfter;

  function install(router) {
    removeBefore = router.beforeEach(() => {
      cancelRestore();
      const wrap = document.querySelector(".page-scroll-wrap");
      if (currentEntry && wrap) {
        positions.set(currentEntry.position, {
          path: currentEntry.path,
          top: wrap.scrollTop,
          left: wrap.scrollLeft,
        });
      }
      // Popstate updates history before guards; push/replace update it afterwards.
      traversal =
        !!currentEntry && history.state?.position !== currentEntry.position;
      pending = undefined;
    });
    removeAfter = router.afterEach((to, from, failure) => {
      if (failure) return;
      const position = history.state?.position;
      const saved = traversal && positions.get(position);
      pending = {
        route: to,
        saved: saved?.path === to.fullPath ? saved : undefined,
      };
      currentEntry = { position, path: to.fullPath };
      // A new push after going back replaces all forward entries.
      if (!traversal) {
        for (const key of positions.keys()) {
          if (key >= position) positions.delete(key);
        }
      }
    });
  }

  function scrollBehavior(to) {
    if (pending?.route !== to) return false;
    const { saved } = pending;
    pending = undefined;
    cancelRestore();

    let frame;
    let resizeObserver;
    let observedWrap;
    let finished = false;
    const cleanup = () => {
      finished = true;
      clearTimeout(timeout);
      if (frame !== undefined) cancelAnimationFrame(frame);
      mutationObserver.disconnect();
      resizeObserver?.disconnect();
      for (const event of ["wheel", "touchstart", "pointerdown", "keydown"]) {
        document.removeEventListener(event, cancelOnInput, true);
      }
    };
    const cancelOnInput = (event) => {
      if (event.type !== "keydown" || SCROLL_KEYS.has(event.key)) cleanup();
    };
    const attempt = () => {
      frame = undefined;
      if (finished) return;
      const wrap = document.querySelector(".page-scroll-wrap");
      if (!wrap) return;
      if (wrap !== observedWrap && typeof ResizeObserver !== "undefined") {
        resizeObserver?.disconnect();
        resizeObserver = new ResizeObserver(schedule);
        resizeObserver.observe(wrap);
        const content = wrap.querySelector(".page-scroll-content");
        if (content) resizeObserver.observe(content);
        observedWrap = wrap;
      }

      let top = saved?.top ?? 0;
      if (!saved && to.hash) {
        let id;
        try {
          id = decodeURIComponent(to.hash.slice(1));
        } catch {
          id = to.hash.slice(1);
        }
        const anchor = document.getElementById(id);
        if (!anchor || !wrap.contains(anchor)) {
          wrap.scrollTop = 0;
          return;
        }
        const margin =
          parseFloat(getComputedStyle(anchor).scrollMarginTop) || 0;
        top = Math.max(
          0,
          anchor.getBoundingClientRect().top -
            wrap.getBoundingClientRect().top +
            wrap.scrollTop -
            margin,
        );
      }
      wrap.scrollTop = top;
      wrap.scrollLeft = saved?.left ?? 0;
      // Loading placeholders may not yet provide enough height to restore.
      if (wrap.scrollHeight - wrap.clientHeight >= top - 1) cleanup();
    };
    const schedule = () => {
      if (!finished && frame === undefined)
        frame = requestAnimationFrame(attempt);
    };
    const mutationObserver = new MutationObserver(schedule);
    mutationObserver.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["id"],
    });
    const timeout = setTimeout(cleanup, RESTORE_TIMEOUT);
    for (const event of ["wheel", "touchstart", "pointerdown", "keydown"]) {
      document.addEventListener(event, cancelOnInput, {
        capture: true,
        passive: true,
      });
    }
    cancelRestore = cleanup;
    attempt();
    return false;
  }

  function dispose() {
    cancelRestore();
    removeBefore?.();
    removeAfter?.();
    positions.clear();
  }

  return { scrollBehavior, install, dispose };
}
