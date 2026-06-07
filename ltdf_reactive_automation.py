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

    spin_button: str = ".ant-btn-circle, [class*='spin-button'], #spin_btn, [class*='SpinButton']"
    turbo_button: str = "[class*='turbo-button'], #turbo_btn, [class*='TurboButton']"


@dataclass(frozen=True)
class LTDFReactiveConfig:
    """Runtime controls for safe injection."""

    dom_timeout_seconds: int = 30
    loading_poll_ms: int = 1000
    observer_cooldown_ms: int = 40
    watchdog_interval_ms: int = 1500
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
              if (typeof window.__LTDF_SPEED_DESTROY__ === "function") {
                return window.__LTDF_SPEED_DESTROY__("python_destroy");
              }
              if (typeof window.__LTDF_SNIPER_DESTROY__ === "function") {
                return window.__LTDF_SNIPER_DESTROY__("python_destroy_legacy");
              }
              if (window.__LTDF_CHECK_INTERVAL__) clearInterval(window.__LTDF_CHECK_INTERVAL__);
              window.__LTDF_CHECK_INTERVAL__ = null;
              if (window.__LTDF_SPEED_OBSERVER__) window.__LTDF_SPEED_OBSERVER__.disconnect();
              window.__LTDF_SPEED_OBSERVER__ = null;
              if (window.__LTDF_SPEED_FALLBACK_INTERVAL__) clearInterval(window.__LTDF_SPEED_FALLBACK_INTERVAL__);
              window.__LTDF_SPEED_FALLBACK_INTERVAL__ = null;
              window.__LTDF_SPEED_ACTIVE__ = false;
              if (window.__LTDF_SNIPER_OBSERVER__) window.__LTDF_SNIPER_OBSERVER__.disconnect();
              window.__LTDF_SNIPER_OBSERVER__ = null;
              if (window.__LTDF_SNIPER_FALLBACK_INTERVAL__) clearInterval(window.__LTDF_SNIPER_FALLBACK_INTERVAL__);
              window.__LTDF_SNIPER_FALLBACK_INTERVAL__ = null;
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
    watchdogMs: {int(self.config.watchdog_interval_ms)},
    selectors: {{
      spinButton: {selectors.spin_button!r},
      turboButton: {selectors.turbo_button!r}
    }}
  }};

  function destroy(reason) {{
    try {{ if (window.__LTDF_CHECK_INTERVAL__) clearInterval(window.__LTDF_CHECK_INTERVAL__); }} catch (_) {{}}
    window.__LTDF_CHECK_INTERVAL__ = null;
    try {{ if (window.__LTDF_SPEED_OBSERVER__) window.__LTDF_SPEED_OBSERVER__.disconnect(); }} catch (_) {{}}
    window.__LTDF_SPEED_OBSERVER__ = null;
    try {{ if (window.__LTDF_SPEED_FALLBACK_INTERVAL__) clearInterval(window.__LTDF_SPEED_FALLBACK_INTERVAL__); }} catch (_) {{}}
    window.__LTDF_SPEED_FALLBACK_INTERVAL__ = null;
    window.__LTDF_SPEED_ACTIVE__ = false;
    window.__LTDF_SPEED_MOTOR_ACTIVE__ = false;
    window.__LTDF_SPEED_LAST_DESTROY__ = {{ reason: reason || "destroy", href: location.href, at: Date.now() }};
    return {{ ok:true, status:"DESTROYED", reason:reason || "destroy" }};
  }}

  window.__LTDF_SPEED_DESTROY__ = destroy;

  if (window.__LTDF_SPEED_ACTIVE__) {{
    console.log("[LTDF] Injetor ja operando nesta aba.");
    return {{ ok:true, status:"JA_ATIVO", href:location.href }};
  }}

  destroy("pre_inject_cleanup");
  window.__LTDF_SPEED_ACTIVE__ = true;
  window.__LTDF_SPEED_MOTOR_ACTIVE__ = false;
  window.__LTDF_SPEED_VERSION__ = "reactive_inputs_dynamic_lookup_v1";

  console.log("[LTDF] Injetor acoplado. Aguardando fim do loading...");

  function visible(el) {{
    if (!el || !el.isConnected) return false;
    const rect = el.getBoundingClientRect();
    return !!((el.offsetWidth > 0 || rect.width > 0) && (el.offsetHeight > 0 || rect.height > 0));
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
    const opts = {{ bubbles:true, cancelable:true, composed:true, view:window }};
    try {{ el.dispatchEvent(new MouseEvent("mousedown", opts)); }} catch (_) {{}}
    try {{ el.dispatchEvent(new MouseEvent("mouseup", opts)); }} catch (_) {{}}
    try {{ el.dispatchEvent(new MouseEvent("click", opts)); }} catch (_) {{}}
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

  function clickIfReady(source) {{
    const current = findSpinButton();
    if (enabled(current)) {{
      dispatchNativeClick(current);
      window.__LTDF_SPEED_LAST_CLICK__ = {{ source:source || "ready", at:Date.now() }};
    }}
  }}

  function ativarMotorSpeed() {{
    if (window.__LTDF_SPEED_MOTOR_ACTIVE__) return false;
    window.__LTDF_SPEED_MOTOR_ACTIVE__ = true;
    console.log("[LTDF] Motor Turbo Reativo acionado com busca dinamica.");

    try {{ if (window.__LTDF_SPEED_OBSERVER__) window.__LTDF_SPEED_OBSERVER__.disconnect(); }} catch (_) {{}}
    maybeEnableTurbo();

    const root = document.body || document.documentElement;
    if (!root) return false;

    window.__LTDF_SPEED_OBSERVER__ = new MutationObserver(() => {{
      clickIfReady("mutation_ready");
    }});

    window.__LTDF_SPEED_OBSERVER__.observe(root, {{
      childList:true,
      subtree:true,
      attributes:true
    }});

    try {{ if (window.__LTDF_SPEED_FALLBACK_INTERVAL__) clearInterval(window.__LTDF_SPEED_FALLBACK_INTERVAL__); }} catch (_) {{}}
    window.__LTDF_SPEED_FALLBACK_INTERVAL__ = setInterval(() => {{
      clickIfReady("watchdog_passive");
    }}, CONFIG.watchdogMs);

    clickIfReady("motor_start");
    window.__LTDF_SPEED_READY__ = {{ at:Date.now(), href:location.href }};
    return true;
  }}

  function waitForGameReady() {{
    try {{ if (window.__LTDF_CHECK_INTERVAL__) clearInterval(window.__LTDF_CHECK_INTERVAL__); }} catch (_) {{}}
    window.__LTDF_CHECK_INTERVAL__ = setInterval(() => {{
      const botaoValidacao = findSpinButton();
      if (!visible(botaoValidacao)) return;
      try {{ clearInterval(window.__LTDF_CHECK_INTERVAL__); }} catch (_) {{}}
      window.__LTDF_CHECK_INTERVAL__ = null;
      ativarMotorSpeed();
    }}, CONFIG.pollMs);
  }}

  waitForGameReady();

  window.addEventListener("beforeunload", () => destroy("beforeunload"), {{ once:true }});
  return {{ ok:true, status:"STANDBY_OK", pollMs:CONFIG.pollMs, watchdogMs:CONFIG.watchdogMs, href:location.href }};
}})();
""".strip()


def injetar_motor_ltdf(driver: webdriver.Chrome, config: LTDFReactiveConfig | None = None) -> dict[str, Any]:
    """Compatibility helper for the existing operational flow."""

    return LTDFReactiveInjector(config=config).inject(driver)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    print("Importe LTDFReactiveInjector e conecte-o ao webdriver/AdsPower ativo.")
