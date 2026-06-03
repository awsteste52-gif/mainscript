# LTDF SNIPER - Roadmap de Estabilidade em Lote

## Diagnostico final do modulo Speed

O modulo de aceleracao artificial por clock, renderizacao e clique nativo esta encerrado.

A validacao pratica ate o commit `00a6457` confirmou que a engine opera com Server-Side Tick Rate. O Canvas/WebGL sincroniza a evolucao visual com payloads recebidos via WebSocket, e o servidor retem respostas para cumprir o intervalo minimo de cada rodada.

Por esse motivo, manipulacoes locais de `requestAnimationFrame`, deltas de tempo, `Date.now`, `performance.now`, Workers, WebAssembly ou rajadas de clique nao reduzem o tempo efetivo da rodada. Quando o cliente adianta o proprio relogio, a interface apenas entra em espera pelo proximo pacote do servidor.

## Estado atual

- Speed HTML5 arquivado.
- Configuracao forcada para `enabled=false` e `speed=1.0`.
- Injecao automatica de scripts de Speed removida do manifesto da extensao.
- Rotina restante limitada a limpeza anti-freeze sob demanda.
- Objetivo de aceleracao local considerado concluido por limite fisico e de seguranca do servidor.
- Anti-freeze homologado no commit `00a6457`: o escopo global e os residuos de automacao sao limpos em reentrada, recarregamento ou transicao de abas.

## Prioridades futuras

1. Homologacao da injecao limpa
   Manter o modelo atual leve e destrutivo, sem reinserir hooks de aceleracao. O anti-freeze da barreira dos 6% esta concluido, homologado e deve ser preservado como requisito de estabilidade.

2. Concorrencia da grade AdsPower
   Melhorar o backend Python para abrir, posicionar e coordenar multiplos perfis em lote, com estabilidade de CPU e memoria para 10, 15 ou mais instancias simultaneas.

3. Monitorizacao de RTT e proxy
   Implementar medicao de latencia das conexoes proxy para ajustar loops de automacao de acordo com o tempo real de resposta da rede.

## Diretriz tecnica

Nao retomar aceleracao por clock local sem uma nova evidencia de que a aplicacao deixou de validar tempo no servidor. O foco do projeto passa a ser robustez operacional, escala em lote e sincronismo com rede/proxy.

## Referencia tecnica para motor reativo futuro

Esta referencia pode orientar futuras rotinas de input operacional, sem reabrir o modulo Speed. Ela deve ser usada apenas quando houver seletor real e validacao de que a rotina nao interfere no carregamento do Canvas.

Principios obrigatorios:

- Singleton global para impedir injecoes concorrentes.
- Limpeza de intervalos e observers antes de reentrada.
- Inicializacao somente apos `DOMContentLoaded`.
- Polling inicial leve, entre 800ms e 1000ms.
- Validacao geometrica do alvo antes de ativar o motor.
- Busca dinamica do elemento em cada disparo para evitar nodes orfaos.
- Trava no callback do `MutationObserver` para evitar loop circular.
- Reset em `beforeunload`.

```js
(function () {
  if (window.__LTDF_SNIPER_ACTIVE__) {
    console.warn("[LTDF SNIPER] Instancia ja ativa nesta aba. Ignorando reinjecao.");
    return;
  }
  window.__LTDF_SNIPER_ACTIVE__ = true;

  const SELETORES = {
    botaoGirar: ".ant-btn-circle, [class*='spin-button'], #spin_btn",
    botaoTurbo: "[class*='turbo-button'], #turbo_btn",
  };

  let checagemInicialInterval = null;
  let observerInputs = null;
  let observerBusy = false;

  function limparTemporizadores() {
    try {
      if (checagemInicialInterval) clearInterval(checagemInicialInterval);
    } catch (_) {}
    checagemInicialInterval = null;
    try {
      if (observerInputs) observerInputs.disconnect();
    } catch (_) {}
    observerInputs = null;
    observerBusy = false;
  }

  function estaVisivel(elemento) {
    if (!elemento || !elemento.isConnected) return false;
    const rect = elemento.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function estaLiberado(elemento) {
    if (!estaVisivel(elemento)) return false;
    if (elemento.disabled || elemento.hasAttribute("disabled")) return false;
    if (elemento.classList.contains("disabled")) return false;
    return true;
  }

  function dispararCliqueNativo(elemento) {
    if (!estaLiberado(elemento)) return;
    const opts = { bubbles: true, cancelable: true, view: window };
    elemento.dispatchEvent(new MouseEvent("mousedown", opts));
    elemento.dispatchEvent(new MouseEvent("mouseup", opts));
    elemento.dispatchEvent(new MouseEvent("click", opts));
  }

  function iniciarMotorAutomacao() {
    limparTemporizadores();

    const botaoTurbo = document.querySelector(SELETORES.botaoTurbo);
    if (estaLiberado(botaoTurbo) && !botaoTurbo.classList.contains("active")) {
      dispararCliqueNativo(botaoTurbo);
    }

    observerInputs = new MutationObserver(() => {
      if (observerBusy) return;
      observerBusy = true;
      setTimeout(() => {
        try {
          const btnAtualDinamico = document.querySelector(SELETORES.botaoGirar);
          if (estaLiberado(btnAtualDinamico)) {
            dispararCliqueNativo(btnAtualDinamico);
          }
        } finally {
          observerBusy = false;
        }
      }, 0);
    });

    observerInputs.observe(document.body || document.documentElement, {
      attributes: true,
      childList: true,
      subtree: true,
      attributeFilter: ["class", "disabled"],
    });
  }

  function verificarCarregamentoReal() {
    try {
      if (checagemInicialInterval) clearInterval(checagemInicialInterval);
    } catch (_) {}
    checagemInicialInterval = setInterval(() => {
      const botaoGirar = document.querySelector(SELETORES.botaoGirar);
      if (estaVisivel(botaoGirar)) {
        iniciarMotorAutomacao();
      }
    }, 800);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", verificarCarregamentoReal, { once: true });
  } else {
    verificarCarregamentoReal();
  }

  window.addEventListener("beforeunload", () => {
    limparTemporizadores();
    window.__LTDF_SNIPER_ACTIVE__ = false;
  }, { once: true });
})();
```
