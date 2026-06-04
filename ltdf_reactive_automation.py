"""LTDF reactive input automation helper.

This module keeps the post-Speed architecture focused on operational
stability: DOM readiness, singleton injection, lightweight polling, dynamic
element lookup, and clean teardown. It does not manipulate clocks or try to
accelerate a server-side tick rate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait


LOGGER = logging.getLogger("ltdf.reactive_automation")


@dataclass(frozen=True)
class LTDFSelectors:
    """Selectors used by the browser-side input engine."""

    spin_button: str = ".ant-btn-circle, [class*='spin-button'], #spin_btn"
    turbo_button: str = "[class*='turbo-button'], #turbo_btn"


@dataclass(frozen=True)
class LTDFReactiveConfig:
    """Runtime controls for safe injection."""

    dom_timeout_seconds: int = 30
    loading_poll_ms: int = 1000
    observer_cooldown_ms: int = 40
    watchdog_interval_ms: int = 2500
    click_burst: int = 3
    selectors: LTDFSelectors = field(default_factory=LTDFSelectors)


class LTDFReactiveInjector:
    """Injects a singleton JavaScript input engine into the current tab."""

    def __init__(self, config: LTDFReactiveConfig | None = None, logger: logging.Logger | None = None) -> None:
        self.config = config or LTDFReactiveConfig()
        self.logger = logger or LOGGER

    def wait_for_dom(self, driver: webdriver.Chrome) -> None:
        self.logger.info("Aguardando DOM em estado interactive/complete.")
        WebDriverWait(driver, self.config.dom_timeout_seconds).until(
            lambda d: d.execute_script(
                "return document.readyState === 'complete' || document.readyState === 'interactive';"
            )
        )

    def inject(self, driver: webdriver.Chrome) -> dict[str, Any]:
        """Inject the browser-side engine and return its status payload."""

        self.wait_for_dom(driver)
        self.logger.info("Injetando motor reativo LTDF.")
        result = driver.execute_script(self._build_script())
        if isinstance(result, dict):
            self.logger.info("Motor reativo LTDF: %s", result.get("status", "sem_status"))
            return result
        return {"ok": bool(result), "status": str(result)}

    def destroy(self, driver: webdriver.Chrome) -> dict[str, Any]:
        """Destroy any active browser-side instance in the current tab."""

        result = driver.execute_script(
            """
            try {
              if (typeof window.__LTDF_SNIPER_DESTROY__ === "function") {
                return window.__LTDF_SNIPER_DESTROY__("python_destroy");
              }
              if (window.__LTDF_CHECK_INTERVAL__) clearInterval(window.__LTDF_CHECK_INTERVAL__);
              window.__LTDF_CHECK_INTERVAL__ = null;
              if (window.__LTDF_SNIPER_REBIND_INTERVAL__) clearInterval(window.__LTDF_SNIPER_REBIND_INTERVAL__);
              window.__LTDF_SNIPER_REBIND_INTERVAL__ = null;
              if (window.__LTDF_SNIPER_OBSERVER__) window.__LTDF_SNIPER_OBSERVER__.disconnect();
              window.__LTDF_SNIPER_OBSERVER__ = null;
              window.__LTDF_SNIPER_ACTIVE__ = false;
              return { ok:true, status:"DESTROYED_LEGACY" };
            } catch (error) {
              return { ok:false, status:"DESTROY_FAILED", error:String(error) };
            }
            """
        )
        return result if isinstance(result, dict) else {"ok": bool(result), "status": str(result)}

    def _build_script(self) -> str:
        selectors = self.config.selectors
        return f"""
(function() {{
  "use strict";

  const CONFIG = {{
    pollMs: {int(self.config.loading_poll_ms)},
    observerCooldownMs: {int(self.config.observer_cooldown_ms)},
    watchdogMs: {int(self.config.watchdog_interval_ms)},
    clickBurst: {int(self.config.click_burst)},
    selectors: {{
      spinButton: {selectors.spin_button!r},
      turboButton: {selectors.turbo_button!r}
    }}
  }};

  function destroy(reason) {{
    try {{ if (window.__LTDF_CHECK_INTERVAL__) clearInterval(window.__LTDF_CHECK_INTERVAL__); }} catch (_) {{}}
    window.__LTDF_CHECK_INTERVAL__ = null;
    try {{ if (window.__LTDF_SNIPER_OBSERVER__) window.__LTDF_SNIPER_OBSERVER__.disconnect(); }} catch (_) {{}}
    window.__LTDF_SNIPER_OBSERVER__ = null;
    window.__LTDF_SNIPER_OBSERVER_ROOT__ = null;
    try {{ if (window.__LTDF_SNIPER_REBIND_INTERVAL__) clearInterval(window.__LTDF_SNIPER_REBIND_INTERVAL__); }} catch (_) {{}}
    window.__LTDF_SNIPER_REBIND_INTERVAL__ = null;
    window.__LTDF_SNIPER_OBSERVER_BUSY__ = false;
    window.__LTDF_SNIPER_ACTIVE__ = false;
    window.__LTDF_SNIPER_LAST_DESTROY__ = {{ reason: reason || "destroy", href: location.href, at: Date.now() }};
    return {{ ok:true, status:"DESTROYED", reason:reason || "destroy" }};
  }}

  window.__LTDF_SNIPER_DESTROY__ = destroy;

  if (window.__LTDF_SNIPER_ACTIVE__) {{
    return {{ ok:true, status:"JA_ATIVO", href:location.href }};
  }}

  destroy("pre_inject_cleanup");
  window.__LTDF_SNIPER_ACTIVE__ = true;
  window.__LTDF_SNIPER_VERSION__ = "reactive_inputs_v1";

  function visible(el) {{
    if (!el || !el.isConnected) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }}

  function enabled(el) {{
    if (!visible(el)) return false;
    if (el.disabled || el.hasAttribute("disabled")) return false;
    if (el.getAttribute("aria-disabled") === "true") return false;
    if (el.classList && el.classList.contains("disabled")) return false;
    return true;
  }}

  function dispatchNativeClick(el) {{
    if (!enabled(el)) return false;
    const rect = el.getBoundingClientRect();
    const x = Math.round(rect.left + rect.width / 2);
    const y = Math.round(rect.top + rect.height / 2);
    const pointer = {{
      bubbles:true, cancelable:true, composed:true, view:window,
      clientX:x, clientY:y, screenX:x, screenY:y,
      button:0, buttons:1, pointerId:1, pointerType:"mouse", isPrimary:true
    }};
    const mouseDown = {{
      bubbles:true, cancelable:true, composed:true, view:window,
      clientX:x, clientY:y, screenX:x, screenY:y,
      button:0, buttons:1
    }};
    const mouseUp = {{ ...mouseDown, buttons:0 }};
    try {{ el.dispatchEvent(new PointerEvent("pointerdown", pointer)); }} catch (_) {{}}
    try {{ el.dispatchEvent(new MouseEvent("mousedown", mouseDown)); }} catch (_) {{}}
    try {{ el.dispatchEvent(new PointerEvent("pointerup", {{ ...pointer, buttons:0 }})); }} catch (_) {{}}
    try {{ el.dispatchEvent(new MouseEvent("mouseup", mouseUp)); }} catch (_) {{}}
    try {{ el.dispatchEvent(new MouseEvent("click", mouseUp)); }} catch (_) {{}}
    return true;
  }}

  function findSpinButton() {{
    return document.querySelector(CONFIG.selectors.spinButton);
  }}

  function findTurboButton() {{
    return document.querySelector(CONFIG.selectors.turboButton);
  }}

  function maybeEnableTurbo() {{
    const turbo = findTurboButton();
    if (enabled(turbo) && !(turbo.classList && turbo.classList.contains("active"))) {{
      dispatchNativeClick(turbo);
    }}
  }}

  function clickBurst(source) {{
    const count = Math.max(1, Math.min(3, CONFIG.clickBurst || 1));
    for (let i = 0; i < count; i += 1) {{
      setTimeout(() => {{
        const current = findSpinButton();
        if (enabled(current)) {{
          dispatchNativeClick(current);
          window.__LTDF_SNIPER_LAST_CLICK__ = {{ source:source || "observer", index:i, at:Date.now() }};
        }}
      }}, i * 8);
    }}
  }}

  function observerRoot() {{
    return document.documentElement || document.body;
  }}

  function handleReadyMutation(source) {{
    const current = findSpinButton();
    if (enabled(current)) clickBurst(source || "mutation_ready");
  }}

  function startObserver() {{
    try {{ if (window.__LTDF_SNIPER_OBSERVER__) window.__LTDF_SNIPER_OBSERVER__.disconnect(); }} catch (_) {{}}
    maybeEnableTurbo();

    const root = observerRoot();
    if (!root) return false;

    window.__LTDF_SNIPER_OBSERVER__ = new MutationObserver(() => {{
      if (window.__LTDF_SNIPER_OBSERVER_BUSY__) return;
      window.__LTDF_SNIPER_OBSERVER_BUSY__ = true;
      setTimeout(() => {{
        try {{
          handleReadyMutation("mutation_ready");
        }} finally {{
          window.__LTDF_SNIPER_OBSERVER_BUSY__ = false;
        }}
      }}, CONFIG.observerCooldownMs);
    }});

    window.__LTDF_SNIPER_OBSERVER_ROOT__ = root;
    window.__LTDF_SNIPER_OBSERVER__.observe(root, {{
      attributes:true,
      childList:true,
      subtree:true,
      attributeFilter:["class", "disabled", "aria-disabled"]
    }});
    try {{ if (window.__LTDF_SNIPER_REBIND_INTERVAL__) clearInterval(window.__LTDF_SNIPER_REBIND_INTERVAL__); }} catch (_) {{}}
    window.__LTDF_SNIPER_REBIND_INTERVAL__ = setInterval(() => {{
      const currentRoot = observerRoot();
      if (!currentRoot) return;
      if (!window.__LTDF_SNIPER_OBSERVER__ || window.__LTDF_SNIPER_OBSERVER_ROOT__ !== currentRoot || !window.__LTDF_SNIPER_OBSERVER_ROOT__.isConnected) {{
        startObserver();
        handleReadyMutation("observer_rebind");
      }}
    }}, CONFIG.watchdogMs);
    return true;
  }}

  function waitForGameReady() {{
    try {{ if (window.__LTDF_CHECK_INTERVAL__) clearInterval(window.__LTDF_CHECK_INTERVAL__); }} catch (_) {{}}
    window.__LTDF_CHECK_INTERVAL__ = setInterval(() => {{
      const spin = findSpinButton();
      if (!visible(spin)) return;
      try {{ clearInterval(window.__LTDF_CHECK_INTERVAL__); }} catch (_) {{}}
      window.__LTDF_CHECK_INTERVAL__ = null;
      startObserver();
      window.__LTDF_SNIPER_READY__ = {{ at:Date.now(), href:location.href }};
    }}, CONFIG.pollMs);
  }}

  function boot() {{
    if (document.readyState === "loading") {{
      document.addEventListener("DOMContentLoaded", waitForGameReady, {{ once:true }});
    }} else {{
      waitForGameReady();
    }}
  }}

  window.addEventListener("beforeunload", () => destroy("beforeunload"), {{ once:true }});
  boot();
  return {{ ok:true, status:"INJETADO_COM_SUCESSO", pollMs:CONFIG.pollMs, watchdogMs:CONFIG.watchdogMs, href:location.href }};
}})();
""".strip()


def injetar_motor_ltdf(driver: webdriver.Chrome, config: LTDFReactiveConfig | None = None) -> dict[str, Any]:
    """Compatibility helper for the existing operational flow."""

    return LTDFReactiveInjector(config=config).inject(driver)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    print("Importe LTDFReactiveInjector e conecte-o ao webdriver/AdsPower ativo.")
