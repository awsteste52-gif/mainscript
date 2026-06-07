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
from selenium.webdriver.common.by import By
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
    watchdog_interval_ms: int = 30
    iframe_scan_depth: int = 2
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
        script = self._build_script()
        result = self._inject_in_game_context(driver, script)
        if isinstance(result, dict):
            self.logger.info("Motor reativo LTDF: %s", result.get("status", "sem_status"))
            return result
        return {"ok": bool(result), "status": str(result)}

    def _probe_current_context(self, driver: webdriver.Chrome, path: list[int]) -> dict[str, Any]:
        """Score the active frame so injection lands inside the actual game DOM."""

        try:
            info = driver.execute_script(
                """
                const selector = arguments[0];
                const path = arguments[1];
                const button = document.querySelector(selector);
                const rect = button && button.getBoundingClientRect ? button.getBoundingClientRect() : null;
                const visibleButton = !!(button && (
                  button.offsetWidth > 0 ||
                  button.offsetHeight > 0 ||
                  (rect && (rect.width > 0 || rect.height > 0))
                ));
                const canvas = document.querySelector("canvas");
                const gameLike = document.querySelector("[id*='game' i], [class*='game' i], [id*='canvas' i], [class*='canvas' i]");
                const frameCount = document.querySelectorAll("iframe").length;
                return {
                  path,
                  href: String(location.href || ""),
                  title: String(document.title || ""),
                  hasButton: !!button,
                  visibleButton,
                  hasCanvas: !!canvas,
                  hasGameLike: !!gameLike,
                  frameCount,
                  bodyTextLength: document.body ? String(document.body.innerText || "").length : 0
                };
                """,
                self.config.selectors.spin_button,
                path,
            )
        except Exception as exc:
            return {"path": path, "error": str(exc), "score": -1}

        score = 0
        if info.get("visibleButton"):
            score += 1000
        if info.get("hasButton"):
            score += 700
        if info.get("hasCanvas"):
            score += 250
        if info.get("hasGameLike"):
            score += 150
        if info.get("frameCount"):
            score += min(75, int(info.get("frameCount") or 0) * 10)
        info["score"] = score
        return info

    def _switch_to_frame_path(self, driver: webdriver.Chrome, path: list[int]) -> bool:
        try:
            driver.switch_to.default_content()
            for index in path:
                frames = driver.find_elements(By.TAG_NAME, "iframe")
                if index >= len(frames):
                    return False
                driver.switch_to.frame(frames[index])
            return True
        except Exception:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
            return False

    def _collect_frame_contexts(
        self,
        driver: webdriver.Chrome,
        path: list[int] | None = None,
        depth: int | None = None,
    ) -> list[dict[str, Any]]:
        path = path or []
        depth = int(self.config.iframe_scan_depth if depth is None else depth)
        contexts: list[dict[str, Any]] = []
        if not self._switch_to_frame_path(driver, path):
            return contexts

        contexts.append(self._probe_current_context(driver, path))
        if depth <= 0:
            return contexts

        try:
            frame_count = len(driver.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            frame_count = 0

        for index in range(frame_count):
            contexts.extend(self._collect_frame_contexts(driver, [*path, index], depth - 1))
        return contexts

    def _fallback_largest_iframe_path(self, driver: webdriver.Chrome) -> list[int]:
        try:
            driver.switch_to.default_content()
            index = driver.execute_script(
                """
                const frames = Array.from(document.querySelectorAll("iframe"));
                let best = -1;
                let bestArea = -1;
                frames.forEach((frame, idx) => {
                  const rect = frame.getBoundingClientRect ? frame.getBoundingClientRect() : null;
                  const area = rect ? Math.max(0, rect.width) * Math.max(0, rect.height) : 0;
                  if (area > bestArea) {
                    best = idx;
                    bestArea = area;
                  }
                });
                return best;
                """
            )
            return [int(index)] if isinstance(index, int) and index >= 0 else []
        except Exception:
            return []

    def _inject_known_game_iframe(self, driver: webdriver.Chrome, script: str) -> dict[str, Any] | None:
        """Fast path for PGSoft/game iframes; ignores protected third-party frames."""

        try:
            driver.switch_to.default_content()
        except Exception:
            return None

        try:
            current_url = str(getattr(driver, "current_url", "") or "").lower()
            if "pgsoft-games" in current_url or "pgsoft" in current_url or "loader" in current_url:
                result = driver.execute_script(script)
                if isinstance(result, dict):
                    result["framePath"] = []
                    result["frameScore"] = 1200
                    result["frameReason"] = {"directGameUrl": True}
                return result
        except Exception:
            pass

        try:
            frame_count = len(driver.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            frame_count = 0

        for index in range(frame_count):
            try:
                driver.switch_to.default_content()
                frames = driver.find_elements(By.TAG_NAME, "iframe")
                if index >= len(frames):
                    continue
                src = str(frames[index].get_attribute("src") or "").lower()
                if not any(token in src for token in ("pgsoft", "game", "loader")):
                    continue
                driver.switch_to.frame(frames[index])
                result = driver.execute_script(script)
                if isinstance(result, dict):
                    result["framePath"] = [index]
                    result["frameScore"] = 1100
                    result["frameReason"] = {"iframeSrc": src[:180]}
                return result
            except Exception as exc:
                self.logger.debug("Iframe %s ignorado durante injecao LTDF: %s", index, exc)
                continue
        return None

    def _inject_in_game_context(self, driver: webdriver.Chrome, script: str) -> dict[str, Any]:
        known_result = self._inject_known_game_iframe(driver, script)
        if known_result is not None:
            return known_result

        contexts = self._collect_frame_contexts(driver)
        best = max(contexts, key=lambda item: int(item.get("score") or -1), default={"path": [], "score": -1})
        path = list(best.get("path") or [])

        if int(best.get("score") or -1) <= 0:
            path = self._fallback_largest_iframe_path(driver)

        if not self._switch_to_frame_path(driver, path):
            driver.switch_to.default_content()
            path = []

        result = driver.execute_script(script)
        if isinstance(result, dict):
            result["framePath"] = path
            result["frameScore"] = int(best.get("score") or 0)
            result["frameReason"] = {
                "visibleButton": bool(best.get("visibleButton")),
                "hasButton": bool(best.get("hasButton")),
                "hasCanvas": bool(best.get("hasCanvas")),
                "hasGameLike": bool(best.get("hasGameLike")),
            }
        return result

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
    return true;
  }}

  function dispatchNativeClick(el) {{
    if (!enabled(el)) return false;
    const rect = el.getBoundingClientRect();
    const clientX = Math.round(rect.left + rect.width / 2);
    const clientY = Math.round(rect.top + rect.height / 2);
    const opts = {{
      bubbles:true,
      cancelable:true,
      composed:true,
      view:window,
      clientX,
      clientY,
      screenX:clientX,
      screenY:clientY,
      button:0,
      buttons:1
    }};
    try {{
      el.dispatchEvent(new PointerEvent("pointerdown", {{
        ...opts,
        pointerId:1,
        pointerType:"mouse",
        isPrimary:true
      }}));
    }} catch (_) {{
      try {{ el.dispatchEvent(new MouseEvent("pointerdown", opts)); }} catch (_) {{}}
    }}
    try {{ el.dispatchEvent(new MouseEvent("mousedown", opts)); }} catch (_) {{}}
    try {{
      el.dispatchEvent(new PointerEvent("pointerup", {{
        ...opts,
        buttons:0,
        pointerId:1,
        pointerType:"mouse",
        isPrimary:true
      }}));
    }} catch (_) {{
      try {{ el.dispatchEvent(new MouseEvent("pointerup", {{ ...opts, buttons:0 }})); }} catch (_) {{}}
    }}
    try {{ el.dispatchEvent(new MouseEvent("mouseup", {{ ...opts, buttons:0 }})); }} catch (_) {{}}
    try {{ el.dispatchEvent(new MouseEvent("click", {{ ...opts, buttons:0 }})); }} catch (_) {{}}
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
      clickIfReady("reactive_interval_30ms");
    }}, CONFIG.watchdogMs);

    clickIfReady("motor_start");
    window.__LTDF_SPEED_READY__ = {{ at:Date.now(), href:location.href }};
    return true;
  }}

  function waitForGameReady() {{
    try {{ if (window.__LTDF_CHECK_INTERVAL__) clearInterval(window.__LTDF_CHECK_INTERVAL__); }} catch (_) {{}}
    window.__LTDF_CHECK_INTERVAL__ = setInterval(() => {{
      const botaoValidacao = findSpinButton();
      const rect = botaoValidacao && botaoValidacao.getBoundingClientRect ? botaoValidacao.getBoundingClientRect() : null;
      const renderizado = !!(botaoValidacao && (
        botaoValidacao.offsetWidth > 0 ||
        botaoValidacao.offsetHeight > 0 ||
        (rect && (rect.width > 0 || rect.height > 0))
      ));
      if (!renderizado) return;
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
