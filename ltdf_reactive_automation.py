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
    iframe_scan_depth: int = 4
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
        preload_armed = False
        result = self._inject_in_game_context(driver, script)
        if isinstance(result, dict):
            result["preloadArmed"] = preload_armed
            self.logger.info("Motor reativo LTDF: %s", result.get("status", "sem_status"))
            return result
        return {"ok": bool(result), "status": str(result), "preloadArmed": preload_armed}

    def _arm_document_start_preload(self, driver: webdriver.Chrome, script: str) -> bool:
        """Arm the Time-Hook at document-start without reloading the root page."""

        if float(self.config.speed_multiplier or 1.0) <= 1.0:
            return False

        try:
            driver.switch_to.default_content()
        except Exception:
            return False

        try:
            driver.execute_cdp_cmd("Page.enable", {})
        except Exception:
            pass
        try:
            driver.execute_cdp_cmd("Runtime.enable", {})
        except Exception:
            pass
        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": script + "\n//# sourceURL=ltdf_time_hook_document_start.js"},
            )
            return True
        except Exception as exc:
            self.logger.debug("Falha ao armar preloader LTDF via CDP: %s", exc)
            return False

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
        """Inject into every known PGSoft/game iframe target we can reach."""

        try:
            driver.switch_to.default_content()
        except Exception:
            return None

        injections: list[dict[str, Any]] = []
        try:
            driver.execute_script(
                """
                if (!Array.isArray(window.__LTDF_RELOADED_FRAMES__)) {
                  window.__LTDF_RELOADED_FRAMES__ = [];
                }
                if (!Array.isArray(window.__LTDF_DONE_RELOADED__)) {
                  window.__LTDF_DONE_RELOADED__ = [];
                }
                return true;
                """
            )
        except Exception:
            pass

        def is_game_url(value: str) -> bool:
            value = str(value or "").lower()
            return any(token in value for token in ("pgsoft-games", "pgsoft", "game", "loader"))

        def wait_current_context_ready() -> dict[str, Any]:
            last_state = ""
            for attempt in range(5):
                try:
                    last_state = str(driver.execute_script("return document.readyState;") or "")
                    if last_state in {"interactive", "complete"}:
                        return {"ready": True, "readyState": last_state, "attempts": attempt + 1}
                except Exception as exc:
                    return {"ready": False, "readyState": last_state, "attempts": attempt + 1, "error": str(exc)}
            return {"ready": False, "readyState": last_state, "attempts": 5}

        def capture_frame_hint(path: list[int]) -> dict[str, Any]:
            if not path:
                return {"index": "root"}
            parent_path = path[:-1]
            target_index = path[-1]
            hint: dict[str, Any] = {"index": target_index, "parentPath": parent_path}
            try:
                if not self._switch_to_frame_path(driver, parent_path):
                    return hint
                frames = driver.find_elements(By.TAG_NAME, "iframe")
                if target_index >= len(frames):
                    hint["frameCount"] = len(frames)
                    return hint
                frame = frames[target_index]
                for attr in ("src", "name", "id", "title"):
                    hint[attr] = str(frame.get_attribute(attr) or "")
                hint["frameCount"] = len(frames)
            except Exception as exc:
                hint["error"] = str(exc)
            finally:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass
            return hint

        def switch_to_frame_path_or_hint(path: list[int], frame_hint: dict[str, Any]) -> dict[str, Any]:
            if not path:
                try:
                    driver.switch_to.default_content()
                    return {"ok": True, "mode": "root"}
                except Exception as exc:
                    return {"ok": False, "mode": "root", "error": str(exc)}
            parent_path = list(frame_hint.get("parentPath") or path[:-1])
            target_index = int(frame_hint.get("index") if isinstance(frame_hint.get("index"), int) else path[-1])
            hint_src = str(frame_hint.get("src") or "").lower()
            hint_name = str(frame_hint.get("name") or "").lower()
            hint_id = str(frame_hint.get("id") or "").lower()
            hint_title = str(frame_hint.get("title") or "").lower()

            def score_frame(frame: Any) -> int:
                try:
                    src = str(frame.get_attribute("src") or "").lower()
                    name = str(frame.get_attribute("name") or "").lower()
                    frame_id = str(frame.get_attribute("id") or "").lower()
                    title = str(frame.get_attribute("title") or "").lower()
                except Exception:
                    return 0
                score = 0
                if hint_src and src == hint_src:
                    score += 100
                elif hint_src and (hint_src in src or src in hint_src):
                    score += 70
                if hint_name and name == hint_name:
                    score += 25
                if hint_id and frame_id == hint_id:
                    score += 25
                if hint_title and title == hint_title:
                    score += 10
                if is_game_url(src):
                    score += 15
                return score

            try:
                if not self._switch_to_frame_path(driver, parent_path):
                    return {"ok": False, "mode": "parent_unavailable", "path": path, "parentPath": parent_path}
                frames = driver.find_elements(By.TAG_NAME, "iframe")
                if target_index < len(frames):
                    indexed_score = score_frame(frames[target_index])
                    if indexed_score > 0 or not any((hint_src, hint_name, hint_id, hint_title)):
                        driver.switch_to.frame(frames[target_index])
                        return {
                            "ok": True,
                            "mode": "path",
                            "path": [*parent_path, target_index],
                            "score": indexed_score,
                            "frameCount": len(frames),
                        }
                best_frame = None
                best_score = -1
                best_index = -1
                for index, frame in enumerate(frames):
                    score = score_frame(frame)
                    if score > best_score:
                        best_frame = frame
                        best_score = score
                        best_index = index
                if best_frame is None or best_score <= 0:
                    return {
                        "ok": False,
                        "mode": "hint_not_found",
                        "path": path,
                        "parentPath": parent_path,
                        "frameCount": len(frames),
                    }
                driver.switch_to.frame(best_frame)
                return {
                    "ok": True,
                    "mode": "hint",
                    "path": [*parent_path, best_index],
                    "score": best_score,
                    "frameCount": len(frames),
                }
            except Exception as exc:
                return {"ok": False, "mode": "hint_error", "path": path, "error": str(exc)}

        def inject_current(path: list[int], reason: dict[str, Any]) -> bool:
            pre_state: dict[str, Any] = {}
            try:
                frame_key = ".".join(str(item) for item in path) if path else "root"
                try:
                    driver.switch_to.default_content()
                    root_state = driver.execute_script(
                        """
                        const key = String(arguments[0] || "root");
                        if (!Array.isArray(window.__LTDF_RELOADED_FRAMES__)) {
                          window.__LTDF_RELOADED_FRAMES__ = [];
                        }
                        if (!Array.isArray(window.__LTDF_DONE_RELOADED__)) {
                          window.__LTDF_DONE_RELOADED__ = [];
                        }
                        const alreadyReloaded =
                          window.__LTDF_RELOADED_FRAMES__.includes(key) ||
                          window.__LTDF_DONE_RELOADED__.includes(key);
                        return { frameKey:key, alreadyReloaded };
                        """,
                        frame_key,
                    ) or {}
                except Exception:
                    root_state = {"frameKey": frame_key, "alreadyReloaded": False}
                frame_hint = capture_frame_hint(path)
                switch_info = switch_to_frame_path_or_hint(path, frame_hint)
                if not switch_info.get("ok"):
                    return False
                ready_info = wait_current_context_ready()
                pre_state = driver.execute_script(
                    """
                    const alreadyReloaded = !!arguments[0];
                    const firstRun = window.__LTDF_SPEED_CONTAINER__ === undefined;
                    if (window.__LTDF_INITIALIZED__ === undefined) window.__LTDF_INITIALIZED__ = true;
                    return {
                      firstRun,
                      reloadDone: alreadyReloaded,
                      href: String(location.href || "")
                    };
                    """,
                    bool(root_state.get("alreadyReloaded")),
                ) or {}
                pre_state["switchInfo"] = switch_info
                result = driver.execute_script(script)
                payload = result if isinstance(result, dict) else {"ok": bool(result), "status": str(result)}
                payload["framePath"] = list(path)
                payload["frameKey"] = frame_key
                payload["frameScore"] = 1200 if not path else 1100
                payload["frameReason"] = reason
                payload["readyInfo"] = ready_info
                payload["iframeFirstRun"] = bool(pre_state.get("firstRun"))
                payload["iframeReloadDone"] = bool(pre_state.get("reloadDone"))
                payload["iframeReloadSent"] = False
                payload["hotSwapOnly"] = True
                payload["href"] = pre_state.get("href")
                injections.append(payload)
            except Exception as exc:
                self.logger.debug("Falha ao injetar LTDF em path %s: %s", path, exc)
            finally:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass
            return False

        try:
            current_url = str(getattr(driver, "current_url", "") or "").lower()
            if is_game_url(current_url):
                inject_current([], {"directGameUrl": True, "url": current_url[:180]})
        except Exception:
            pass

        def scan_frame_tree(path: list[int], depth: int) -> None:
            if depth < 0:
                return
            try:
                if not self._switch_to_frame_path(driver, path):
                    return
                frame_count = len(driver.find_elements(By.TAG_NAME, "iframe"))
            except Exception as exc:
                self.logger.debug("Arvore iframe %s ignorada durante varredura LTDF: %s", path, exc)
                return
            finally:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass

            for index in range(frame_count):
                child_path = [*path, index]
                src = ""
                try:
                    if not self._switch_to_frame_path(driver, path):
                        continue
                    frames = driver.find_elements(By.TAG_NAME, "iframe")
                    if index >= len(frames):
                        continue
                    src = str(frames[index].get_attribute("src") or "").lower()
                except Exception as exc:
                    self.logger.debug("Iframe %s ignorado durante leitura de src LTDF: %s", child_path, exc)
                    continue
                finally:
                    try:
                        driver.switch_to.default_content()
                    except Exception:
                        pass

                child_reloaded = False
                if self._switch_to_frame_path(driver, child_path):
                    child_reloaded = inject_current(
                        child_path,
                        {
                            "iframeSrc": src[:180],
                            "multiTarget": True,
                            "recursiveCascade": True,
                            "hotSwapOnly": True,
                        },
                    )

                scan_frame_tree(child_path, depth - 1)

        scan_frame_tree([], int(self.config.iframe_scan_depth))
        if not injections:
            return None

        return {
            "ok": True,
            "status": "MULTITARGET_IFRAME_OK",
            "injections": len(injections),
            "iframeReloads": sum(1 for item in injections if item.get("iframeReloadSent")),
            "targets": injections,
            "framePath": [item.get("framePath") for item in injections],
            "frameScore": max(int(item.get("frameScore") or 0) for item in injections),
            "frameReason": {"multiTarget": True},
            "multiplier": injections[-1].get("multiplier"),
        }

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
              if (window.__LTDF_SPEED_RAF_ID__) clearTimeout(window.__LTDF_SPEED_RAF_ID__);
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
      const nativeClearTimeout = window.__LTDF_NATIVE_CLEAR_TIMEOUT__ || window.clearTimeout;
      if (window.__LTDF_CHECK_INTERVAL__) {{
        if (typeof window.__LTDF_CHECK_INTERVAL__ === "object" && window.__LTDF_CHECK_INTERVAL__.id) {{
          nativeClearTimeout(window.__LTDF_CHECK_INTERVAL__.id);
        }} else {{
          nativeClearInterval(window.__LTDF_CHECK_INTERVAL__);
        }}
      }}
    }} catch (_) {{}}
    window.__LTDF_CHECK_INTERVAL__ = null;
    try {{ if (window.__LTDF_SPEED_RAF_ID__) nativeClearTimeout(window.__LTDF_SPEED_RAF_ID__); }} catch (_) {{}}
    window.__LTDF_SPEED_RAF_ID__ = null;
    window.__LTDF_SPEED_ACTIVE__ = false;
    window.__LTDF_SPEED_MOTOR_ACTIVE__ = false;
    window.__LTDF_SPEED_CONTAINER__ = null;
    try {{
      const native = window.__LTDF_NATIVOS__ || {{}};
      if (native.setTimeout) window.setTimeout = native.setTimeout;
      if (native.setInterval) window.setInterval = native.setInterval;
      if (native.clearInterval) window.clearInterval = native.clearInterval;
    }} catch (_) {{}}
    try {{ if (window.__LTDF_NATIVE_SET_TIMEOUT__) window.setTimeout = window.__LTDF_NATIVE_SET_TIMEOUT__; }} catch (_) {{}}
    try {{ if (window.__LTDF_NATIVE_SET_INTERVAL__) window.setInterval = window.__LTDF_NATIVE_SET_INTERVAL__; }} catch (_) {{}}
    window.__LTDF_SPEED_LAST_DESTROY__ = {{ reason: reason || "cleanup", href: location.href, at: Date.now() }};
    return {{ ok:true, status:"DESTROYED", reason:reason || "cleanup" }};
  }}

  function teardownBeforeUnload() {{
    try {{
      const native = window.__LTDF_NATIVOS__ || {{}};
      const nativeClearTimeout = native.clearTimeout || window.__LTDF_NATIVE_CLEAR_TIMEOUT__ || window.clearTimeout;
      const nativeClearInterval = native.clearInterval || window.__LTDF_NATIVE_CLEAR_INTERVAL__ || window.clearInterval;
      if (window.__LTDF_CHECK_INTERVAL__) {{
        if (typeof window.__LTDF_CHECK_INTERVAL__ === "object" && window.__LTDF_CHECK_INTERVAL__.id) {{
          nativeClearTimeout(window.__LTDF_CHECK_INTERVAL__.id);
        }} else {{
          nativeClearInterval(window.__LTDF_CHECK_INTERVAL__);
        }}
      }}
      window.__LTDF_CHECK_INTERVAL__ = null;
      if (window.__LTDF_SPEED_RAF_ID__) nativeClearTimeout(window.__LTDF_SPEED_RAF_ID__);
      window.__LTDF_SPEED_RAF_ID__ = null;
      window.__LTDF_SPEED_ACTIVE__ = false;
      window.__LTDF_SPEED_MOTOR_ACTIVE__ = false;
      if (native.setTimeout) window.setTimeout = native.setTimeout;
      if (native.setInterval) window.setInterval = native.setInterval;
      if (native.clearInterval) window.clearInterval = native.clearInterval;
    }} catch (_) {{}}
  }}

  window.__LTDF_SPEED_DESTROY__ = cleanup;
  if (!window.__LTDF_NATIVOS__) {{
    window.__LTDF_NATIVOS__ = {{
      DateNow: window.__LTDF_NATIVE_DATE_NOW__ || Date.now.bind(Date),
      setTimeout: window.__LTDF_NATIVE_SET_TIMEOUT__ || window.setTimeout.bind(window),
      clearTimeout: window.__LTDF_NATIVE_CLEAR_TIMEOUT__ || window.clearTimeout.bind(window),
      setInterval: window.__LTDF_NATIVE_SET_INTERVAL__ || window.setInterval.bind(window),
      clearInterval: window.__LTDF_NATIVE_CLEAR_INTERVAL__ || window.clearInterval.bind(window)
    }};
  }}

  if (
    window.__LTDF_SPEED_CONTAINER__ &&
    MULTIPLICADOR_SPEED > 1.0 &&
    (!window.__LTDF_MODIFICADOS__ || !window.__LTDF_MODIFICADOS__.setTimeout || !window.__LTDF_MODIFICADOS__.setInterval)
  ) {{
    console.log("[LTDF] Atualizando motor para divisao parametrica de timers.");
    cleanup("upgrade_timer_core_patch");
  }}

  if (window.__LTDF_SPEED_CONTAINER__) {{
    window.__LTDF_SPEED_CONTAINER__.multiplicador = MULTIPLICADOR_SPEED;
    if (MULTIPLICADOR_SPEED <= 1.0) {{
      window.__LTDF_SPEED_ACTIVE__ = false;
      window.__LTDF_SPEED_MOTOR_ACTIVE__ = false;
      try {{
        window.setTimeout = window.__LTDF_NATIVOS__.setTimeout;
        window.setInterval = window.__LTDF_NATIVOS__.setInterval;
        window.clearInterval = window.__LTDF_NATIVOS__.clearInterval;
      }} catch (_) {{}}
      console.log("[LTDF] Sistema restaurado para a velocidade normal de fabrica.");
      return {{ ok:true, status:"SPEED_RESTORED_TO_NORMAL", href:location.href, multiplier:MULTIPLICADOR_SPEED }};
    }}
    window.__LTDF_SPEED_ACTIVE__ = true;
    window.__LTDF_SPEED_MOTOR_ACTIVE__ = !!window.__LTDF_SPEED_MOTOR_ACTIVE__;
    if (window.__LTDF_MODIFICADOS__) {{
      window.setTimeout = window.__LTDF_MODIFICADOS__.setTimeout;
      window.setInterval = window.__LTDF_MODIFICADOS__.setInterval;
      window.clearInterval = window.__LTDF_MODIFICADOS__.clearInterval;
    }}
    console.log("[LTDF] Atualizando multiplicador de velocidade para: " + MULTIPLICADOR_SPEED + "x");
    return {{ ok:true, status:"SPEED_UPDATED_DYNAMICALLY", href:location.href, multiplier:MULTIPLICADOR_SPEED }};
  }}

  if (window.__LTDF_SPEED_ACTIVE__) {{
    console.log("[LTDF] Injetor legado ativo; reiniciando para container dinamico.");
    cleanup("legacy_reinject_dynamic_container");
  }}

  if (MULTIPLICADOR_SPEED <= 1.0) {{
    cleanup("speed_1x");
    return {{ ok:true, status:"SPEED_1X_STANDBY", href:location.href, multiplier:MULTIPLICADOR_SPEED }};
  }}

  window.__LTDF_SPEED_ACTIVE__ = true;
  window.__LTDF_SPEED_MOTOR_ACTIVE__ = false;
  window.__LTDF_SPEED_VERSION__ = "dynamic_speed_container_v5";
  window.__LTDF_SPEED_CONTAINER__ = {{ multiplicador: MULTIPLICADOR_SPEED }};

  const setTimeoutOriginal = window.__LTDF_NATIVOS__.setTimeout;
  const clearTimeoutOriginal = window.__LTDF_NATIVOS__.clearTimeout || window.clearTimeout.bind(window);
  const setIntervalOriginal = window.__LTDF_NATIVOS__.setInterval;
  const clearIntervalOriginal = window.__LTDF_NATIVOS__.clearInterval || window.clearInterval.bind(window);

  window.__LTDF_NATIVE_SET_TIMEOUT__ = setTimeoutOriginal;
  window.__LTDF_NATIVE_DATE_NOW__ = window.__LTDF_NATIVOS__.DateNow;
  window.__LTDF_NATIVE_CLEAR_TIMEOUT__ = clearTimeoutOriginal;
  window.__LTDF_NATIVE_SET_INTERVAL__ = setIntervalOriginal;
  window.__LTDF_NATIVE_CLEAR_INTERVAL__ = clearIntervalOriginal;

  function currentSpeed() {{
    const raw = window.__LTDF_SPEED_CONTAINER__ ? Number(window.__LTDF_SPEED_CONTAINER__.multiplicador) : 1.0;
    return Number.isFinite(raw) && raw > 0 ? raw : 1.0;
  }}

  const customTimeout = function(callback, delay, ...args) {{
    return setTimeoutOriginal(callback, Math.max(0, Number(delay || 0) / currentSpeed()), ...args);
  }};

  const customInterval = function(callback, delay, ...args) {{
    const intervalRef = {{ id:null, active:true }};
    const baseDelay = Math.max(1, Number(delay || 0) || 0);
    const cicloInterno = function() {{
      if (!intervalRef.active) return;
      if (typeof callback === "function") callback(...args);
      if (intervalRef.active && window.__LTDF_SPEED_ACTIVE__) {{
        const proximoDelay = Math.max(1, baseDelay / currentSpeed());
        intervalRef.id = setTimeoutOriginal(cicloInterno, proximoDelay);
      }}
    }};
    intervalRef.id = setTimeoutOriginal(cicloInterno, Math.max(1, baseDelay / currentSpeed()));
    return intervalRef;
  }};

  window.clearInterval = function(ref) {{
    if (ref && typeof ref === "object" && "id" in ref) {{
      ref.active = false;
      if (ref.id) clearTimeoutOriginal(ref.id);
      return;
    }}
    return clearIntervalOriginal(ref);
  }};

  window.__LTDF_MODIFICADOS__ = {{
    setTimeout: customTimeout,
    setInterval: customInterval,
    clearInterval: window.clearInterval
  }};

  if (window.__LTDF_SPEED_ACTIVE__) {{
    window.setTimeout = customTimeout;
    window.setInterval = customInterval;
    window.clearInterval = window.__LTDF_MODIFICADOS__.clearInterval;
  }}

  let motorIniciado = false;
  let motorAgendado = false;
  let inputEstavelDesde = 0;

  function dispararCliqueNativo(el) {{
    if (!el || !el.isConnected) return false;
    const rect = el.getBoundingClientRect();
    if (!rect || rect.width === 0 || rect.height === 0) return false;

    const parametrosEvent = {{
      bubbles: true,
      cancelable: true,
      composed: true,
      view: window,
      button: 0,
      buttons: 1
    }};

    try {{ el.dispatchEvent(new PointerEvent("pointerdown", {{ ...parametrosEvent, pointerId:1, pointerType:"mouse", isPrimary:true }})); }} catch (_) {{}}
    try {{ el.dispatchEvent(new MouseEvent("mousedown", parametrosEvent)); }} catch (_) {{}}
    try {{ el.dispatchEvent(new PointerEvent("pointerup", {{ ...parametrosEvent, buttons:0, pointerId:1, pointerType:"mouse", isPrimary:true }})); }} catch (_) {{}}
    try {{ el.dispatchEvent(new MouseEvent("mouseup", {{ ...parametrosEvent, buttons:0 }})); }} catch (_) {{}}
    try {{ el.dispatchEvent(new MouseEvent("click", {{ ...parametrosEvent, buttons:0 }})); }} catch (_) {{}}
    window.__LTDF_SPEED_LAST_CLICK__ = {{
      at: window.__LTDF_NATIVOS__.DateNow ? window.__LTDF_NATIVOS__.DateNow() : Date.now(),
      width: rect.width,
      height: rect.height
    }};
    return true;
  }}

  function estaElementoAcionavel(el) {{
    if (!el || !el.isConnected) return false;
    const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
    if (!rect || rect.width === 0 || rect.height === 0) return false;
    const estilo = window.getComputedStyle ? window.getComputedStyle(el) : null;
    if (estilo && (
      estilo.pointerEvents === "none" ||
      estilo.opacity === "0" ||
      Number(estilo.opacity || 1) <= 0
    )) return false;
    const classes = el.classList;
    const desabilitado =
      el.hasAttribute("disabled") ||
      el.getAttribute("aria-disabled") === "true" ||
      el.getAttribute("data-disabled") === "true" ||
      (classes && (
        classes.contains("disabled") ||
        classes.contains("is-disabled") ||
        classes.contains("ant-btn-disabled")
      ));
    return !desabilitado;
  }}

  function buscarElementoProfundo(seletor, root = document, visitados = new Set()) {{
    if (!root || visitados.has(root)) return null;
    visitados.add(root);
    try {{
      const direto = root.querySelector ? root.querySelector(seletor) : null;
      if (direto && estaElementoAcionavel(direto)) return direto;
      const candidatos = root.querySelectorAll ? root.querySelectorAll("*") : [];
      for (const node of candidatos) {{
        if (node.matches && node.matches(seletor) && estaElementoAcionavel(node)) return node;
        if (node.shadowRoot) {{
          const encontrado = buscarElementoProfundo(seletor, node.shadowRoot, visitados);
          if (encontrado) return encontrado;
        }}
      }}
    }} catch (_) {{}}
    return null;
  }}

  function ativarMotorSpeed() {{
    if (motorIniciado || motorAgendado) return false;
    motorAgendado = true;

    const tentarAtivarMotor = () => {{
      if (!window.__LTDF_SPEED_ACTIVE__) {{
        motorAgendado = false;
        inputEstavelDesde = 0;
        return;
      }}

      const candidato = buscarElementoProfundo(SELETORES.botaoGirar);
      if (!estaElementoAcionavel(candidato)) {{
        inputEstavelDesde = 0;
        setTimeoutOriginal(tentarAtivarMotor, 500);
        return;
      }}

      const agora = window.__LTDF_NATIVOS__.DateNow ? window.__LTDF_NATIVOS__.DateNow() : Date.now();
      if (!inputEstavelDesde) {{
        inputEstavelDesde = agora;
      }}
      if (agora - inputEstavelDesde < 2000) {{
        setTimeoutOriginal(tentarAtivarMotor, 500);
        return;
      }}

      motorAgendado = false;
      motorIniciado = true;
      window.__LTDF_SPEED_MOTOR_ACTIVE__ = true;
      console.log("[LTDF] Motor de aceleracao de hardware sincronizado!");

      const loopExecucaoRapida = () => {{
        if (!window.__LTDF_SPEED_ACTIVE__) {{
          motorIniciado = false;
          motorAgendado = false;
          window.__LTDF_SPEED_MOTOR_ACTIVE__ = false;
          return;
        }}
        const btnAtual = buscarElementoProfundo(SELETORES.botaoGirar);
        if (estaElementoAcionavel(btnAtual)) {{
          dispararCliqueNativo(btnAtual);
          const btnTurbo = buscarElementoProfundo(SELETORES.botaoTurbo);
          const turboAtivo = btnTurbo && btnTurbo.classList && (
            btnTurbo.classList.contains("active") ||
            btnTurbo.classList.contains("is-active")
          );
          if (estaElementoAcionavel(btnTurbo) && !turboAtivo) {{
            dispararCliqueNativo(btnTurbo);
          }}
        }}
        if (window.__LTDF_SPEED_ACTIVE__) {{
          window.__LTDF_SPEED_RAF_ID__ = setTimeoutOriginal(loopExecucaoRapida, 150);
        }}
      }};
      window.__LTDF_SPEED_RAF_ID__ = setTimeoutOriginal(loopExecucaoRapida, 150);
    }};

    setTimeoutOriginal(tentarAtivarMotor, 500);
    return true;
  }}

  window.__LTDF_CHECK_INTERVAL__ = setIntervalOriginal(() => {{
    if (window.__LTDF_SPEED_ACTIVE__) {{
      const botaoValidacao = buscarElementoProfundo(SELETORES.botaoGirar);
      if (botaoValidacao && (botaoValidacao.offsetWidth > 0 || botaoValidacao.getBoundingClientRect().width > 0)) {{
        ativarMotorSpeed();
      }}
    }}
  }}, Math.max(250, POLL_MS));

  window.addEventListener("beforeunload", () => {{
    teardownBeforeUnload();
  }}, {{ once:true }});

  return {{
    ok:true,
    status:"DYNAMIC_SPEED_ENGINE_READY",
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
