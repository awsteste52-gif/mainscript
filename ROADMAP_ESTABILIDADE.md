# LTDF SNIPER - Roadmap de Estabilidade em Lote

## Diagnostico final do modulo Speed

O modulo de aceleracao artificial por clock, renderizacao e clique nativo esta encerrado.

A validacao pratica ate o commit `587aa0f` confirmou que a engine opera com Server-Side Tick Rate. O Canvas/WebGL sincroniza a evolucao visual com payloads recebidos via WebSocket, e o servidor retem respostas para cumprir o intervalo minimo de cada rodada.

Por esse motivo, manipulacoes locais de `requestAnimationFrame`, deltas de tempo, `Date.now`, `performance.now`, Workers, WebAssembly ou rajadas de clique nao reduzem o tempo efetivo da rodada. Quando o cliente adianta o proprio relogio, a interface apenas entra em espera pelo proximo pacote do servidor.

## Estado atual

- Speed HTML5 arquivado.
- Configuracao forcada para `enabled=false` e `speed=1.0`.
- Injecao automatica de scripts de Speed removida do manifesto da extensao.
- Rotina restante limitada a limpeza anti-freeze sob demanda.
- Objetivo de aceleracao local considerado concluido por limite fisico e de seguranca do servidor.

## Prioridades futuras

1. Anti-freeze e ciclo de vida limpo
   Garantir que qualquer residuo de automacao seja destruido em recarregamento, troca de aba ou reentrada no jogo, evitando retencao de memoria e travamento na barreira dos 6%.

2. Concorrencia da grade AdsPower
   Melhorar o backend Python para abrir, posicionar e coordenar multiplos perfis em lote, com estabilidade de CPU e memoria para 10, 15 ou mais instancias simultaneas.

3. Monitorizacao de RTT e proxy
   Implementar medicao de latencia das conexoes proxy para ajustar loops de automacao de acordo com o tempo real de resposta da rede.

## Diretriz tecnica

Nao retomar aceleracao por clock local sem uma nova evidencia de que a aplicacao deixou de validar tempo no servidor. O foco do projeto passa a ser robustez operacional, escala em lote e sincronismo com rede/proxy.
