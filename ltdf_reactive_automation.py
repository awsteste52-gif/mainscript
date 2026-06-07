"""LTDF reactive input automation helper.

This module injects the Speed engine into the active game context. The current
engine combines DOM readiness, dynamic iframe targeting, virtual time hooks,
and synchronized native click dispatch.
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
    iframe_scan_depth: int = 2
    speed_multiplier: float = 4.0
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
              if (window.__LTDF_SPEED_RAF_ID__) cancelAnimationFrame(window.__LTDF_SPEED_RAF_ID__);
              window.__LTDF_SPEED_RAF_ID__ = null;
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
        multiplier = max(1.0, min(10.0, float(self.config.speed_multiplier or 1.0)))
        return f"""
(function() {{
  "use strict";

  const MULTIPLICADOR_SPEED = {multiplier!r};
  const POLL_MS = {int(self.config.loading_poll_ms)};
  const SELETORES = {{
    botaoGirar: {selectors.spin_button!r},
    botaoTurbo: {selectors.turbo_button!r}
  }};

  function cleanup(reason) {{
    try {{
      const nativeClearInterval = window.__LTDF_NATIVE_CLEAR_INTERVAL__ || window.clearInterval;
      if (window.__LTDF_CHECK_INTERVAL__) nativeClearInterval(window.__LTDF_CHECK_INTERVAL__);
    }} catch (_) {{}}
    window.__LTDF_CHECK_INTERVAL__ = null;
    try {{ if (window.__LTDF_SPEED_RAF_ID__) cancelAnimationFrame(window.__LTDF_SPEED_RAF_ID__); }} catch (_) {{}}
    window.__LTDF_SPEED_RAF_ID__ = null;
    window.__LTDF_SPEED_ACTIVE__ = false;
    window.__LTDF_SPEED_MOTOR_ACTIVE__ = false;
    try {{ if (window.__LTDF_NATIVE_DATE__) window.Date = window.__LTDF_NATIVE_DATE__; }} catch (_) {{}}
    try {{
      if (window.__LTDF_NATIVE_PERF_NOW__ && window.performance) {{
        Object.defineProperty(window.performance, "now", {{
          value: window.__LTDF_NATIVE_PERF_NOW__,
          configurable: true,
          writable: true
        }});
      }}
    }} catch (_) {{}}
    try {{ if (window.__LTDF_NATIVE_SET_TIMEOUT__) window.setTimeout = window.__LTDF_NATIVE_SET_TIMEOUT__; }} catch (_) {{}}
    try {{ if (window.__LTDF_NATIVE_SET_INTERVAL__) window.setInterval = window.__LTDF_NATIVE_SET_INTERVAL__; }} catch (_) {{}}
    window.__LTDF_SPEED_LAST_DESTROY__ = {{ reason: reason || "cleanup", href: location.href, at: Date.now() }};
    return {{ ok:true, status:"DESTROYED", reason:reason || "cleanup" }};
  }}

  window.__LTDF_SPEED_DESTROY__ = cleanup;
  if (window.__LTDF_SPEED_ACTIVE__) {{
    console.log("[LTDF] Injetor de tempo e clique ja ativo.");
    cleanup("reinject_time_hook");
  }}

  if (MULTIPLICADOR_SPEED <= 1.0) {{
    cleanup("speed_1x");
    return {{ ok:true, status:"SPEED_1X_STANDBY", href:location.href, multiplier:MULTIPLICADOR_SPEED }};
  }}

  window.__LTDF_SPEED_ACTIVE__ = true;
  window.__LTDF_SPEED_MOTOR_ACTIVE__ = false;
  window.__LTDF_SPEED_VERSION__ = "time_hook_click_sync_v2";

  const DateOriginal = window.__LTDF_NATIVE_DATE__ || window.Date;
  const performanceNowOriginal = window.__LTDF_NATIVE_PERF_NOW__ ||
    (window.performance && typeof window.performance.now === "function" ? window.performance.now.bind(window.performance) : null);
  const setTimeoutOriginal = window.__LTDF_NATIVE_SET_TIMEOUT__ || window.setTimeout.bind(window);
  const setIntervalOriginal = window.__LTDF_NATIVE_SET_INTERVAL__ || window.setInterval.bind(window);
  const clearIntervalOriginal = window.__LTDF_NATIVE_CLEAR_INTERVAL__ || window.clearInterval.bind(window);

  window.__LTDF_NATIVE_DATE__ = DateOriginal;
  window.__LTDF_NATIVE_PERF_NOW__ = performanceNowOriginal;
  window.__LTDF_NATIVE_SET_TIMEOUT__ = setTimeoutOriginal;
  window.__LTDF_NATIVE_SET_INTERVAL__ = setIntervalOriginal;
  window.__LTDF_NATIVE_CLEAR_INTERVAL__ = clearIntervalOriginal;

  const dataInicioReal = DateOriginal.now();
  const perfInicioReal = performanceNowOriginal ? performanceNowOriginal() : 0;
  let ultimoVirtualDate = dataInicioReal;
  let ultimoVirtualPerf = perfInicioReal;

  function virtualDateNow() {{
    const tempoRealAtual = DateOriginal.now();
    const delta = Math.max(0, tempoRealAtual - dataInicioReal);
    ultimoVirtualDate = Math.max(ultimoVirtualDate + 0.001, dataInicioReal + (delta * MULTIPLICADOR_SPEED));
    return Math.floor(ultimoVirtualDate);
  }}

  function virtualPerfNow() {{
    if (!performanceNowOriginal) return virtualDateNow();
    const tempoRealAtual = performanceNowOriginal();
    const delta = Math.max(0, tempoRealAtual - perfInicioReal);
    ultimoVirtualPerf = Math.max(ultimoVirtualPerf + 0.001, perfInicioReal + (delta * MULTIPLICADOR_SPEED));
    return ultimoVirtualPerf;
  }}

  class LTDFTimeHook extends DateOriginal {{
    constructor(...args) {{
      if (args.length === 0) {{
        super(virtualDateNow());
      }} else {{
        super(...args);
      }}
    }}

    static now() {{
      return virtualDateNow();
    }}

    static parse(...args) {{
      return DateOriginal.parse(...args);
    }}

    static UTC(...args) {{
      return DateOriginal.UTC(...args);
    }}
  }}

  try {{
    Object.defineProperty(LTDFTimeHook, "name", {{
      value: "Date",
      configurable: true
    }});
  }} catch (_) {{}}
  window.Date = LTDFTimeHook;

  if (window.performance && performanceNowOriginal) {{
    try {{
      Object.defineProperty(window.performance, "now", {{
        value: virtualPerfNow,
        configurable: true,
        writable: true
      }});
    }} catch (_) {{}}
  }}

  window.setTimeout = function(callback, delay, ...args) {{
    return setTimeoutOriginal(callback, Math.max(0, Number(delay || 0) / MULTIPLICADOR_SPEED), ...args);
  }};

  window.setInterval = function(callback, delay, ...args) {{
    return setIntervalOriginal(callback, Math.max(1, Number(delay || 0) / MULTIPLICADOR_SPEED), ...args);
  }};

  let motorIniciado = false;

  function dispararCliqueNativo(el) {{
    if (!el || !el.isConnected) return false;
    const rect = el.getBoundingClientRect();
    if (!rect || rect.width === 0 || rect.height === 0) return false;

    const clientX = rect.left + (rect.width / 2);
    const clientY = rect.top + (rect.height / 2);
    const parametrosEvent = {{
      bubbles: true,
      cancelable: true,
      composed: true,
      view: window,
      clientX,
      clientY,
      screenX: clientX,
      screenY: clientY,
      button: 0,
      buttons: 1
    }};

    try {{ el.dispatchEvent(new MouseEvent("mousedown", parametrosEvent)); }} catch (_) {{}}
    try {{ el.dispatchEvent(new PointerEvent("pointerdown", {{ ...parametrosEvent, pointerId:1, pointerType:"mouse", isPrimary:true }})); }} catch (_) {{}}
    try {{ el.dispatchEvent(new MouseEvent("mouseup", {{ ...parametrosEvent, buttons:0 }})); }} catch (_) {{}}
    try {{ el.dispatchEvent(new PointerEvent("pointerup", {{ ...parametrosEvent, buttons:0, pointerId:1, pointerType:"mouse", isPrimary:true }})); }} catch (_) {{}}
    try {{ el.dispatchEvent(new MouseEvent("click", {{ ...parametrosEvent, buttons:0 }})); }} catch (_) {{}}
    window.__LTDF_SPEED_LAST_CLICK__ = {{ at: virtualDateNow(), perf: virtualPerfNow(), x: clientX, y: clientY }};
    return true;
  }}

  function ativarMotorSpeed() {{
    if (motorIniciado) return false;
    motorIniciado = true;
    window.__LTDF_SPEED_MOTOR_ACTIVE__ = true;
    console.log("[LTDF] Motor Grafico e Hook de Tempo Sincronizados!");

    const btnTurbo = document.querySelector(SELETORES.botaoTurbo);
    if (btnTurbo && !(btnTurbo.classList && btnTurbo.classList.contains("active"))) {{
      dispararCliqueNativo(btnTurbo);
    }}

    const loopExecucaoRapida = () => {{
      const btnAtual = document.querySelector(SELETORES.botaoGirar);
      if (btnAtual) dispararCliqueNativo(btnAtual);
      if (window.__LTDF_SPEED_ACTIVE__) {{
        window.__LTDF_SPEED_RAF_ID__ = requestAnimationFrame(loopExecucaoRapida);
      }}
    }};
    window.__LTDF_SPEED_RAF_ID__ = requestAnimationFrame(loopExecucaoRapida);
    return true;
  }}

  window.__LTDF_CHECK_INTERVAL__ = setIntervalOriginal(() => {{
    const botaoValidacao = document.querySelector(SELETORES.botaoGirar);
    if (botaoValidacao && (botaoValidacao.offsetWidth > 0 || botaoValidacao.getBoundingClientRect().width > 0)) {{
      clearIntervalOriginal(window.__LTDF_CHECK_INTERVAL__);
      window.__LTDF_CHECK_INTERVAL__ = null;
      ativarMotorSpeed();
    }}
  }}, POLL_MS);

  window.addEventListener("beforeunload", () => {{
    window.__LTDF_SPEED_ACTIVE__ = false;
  }}, {{ once:true }});

  return {{
    ok:true,
    status:"TIME_HOOK_AND_SPEED_OK",
    href:location.href,
    multiplier:MULTIPLICADOR_SPEED,
    pollMs:POLL_MS
  }};
}})();
""".strip()


def injetar_motor_ltdf(
    driver: webdriver.Chrome,
    config: LTDFReactiveConfig | None = None,
    multiplier: float | None = None,
) -> dict[str, Any]:
    """Compatibility helper for the existing operational flow."""

    if multiplier is not None:
        base = config or LTDFReactiveConfig()
        config = LTDFReactiveConfig(
            dom_timeout_seconds=base.dom_timeout_seconds,
            loading_poll_ms=base.loading_poll_ms,
            observer_cooldown_ms=base.observer_cooldown_ms,
            iframe_scan_depth=base.iframe_scan_depth,
            speed_multiplier=float(multiplier),
            selectors=base.selectors,
        )
    return LTDFReactiveInjector(config=config).inject(driver)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    print("Importe LTDFReactiveInjector e conecte-o ao webdriver/AdsPower ativo.")
