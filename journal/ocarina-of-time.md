# Ocarina of Time (Switch 2) no Steam Deck e no Mac: diário de otimização

Diário em andamento. Cada etapa traz o que mudou, como foi medido e o número. Vira post quando terminar.

## O ponto de partida

O jogo é *The Legend of Zelda: Ocarina of Time* de Switch 2 (título `0400486024DEC000`), rodando no [Lichii](https://github.com/zortos293/Lichii), um fork do Eden com o backend nx2 para Switch 2. Este repositório é um fork do Lichii com builds para o Steam Deck: o workflow `deck-build` compila cada `build-N` do upstream com clang 23, PGO e `-march=znver2`, e aplica os patches de `deck/patches/`.

Todas as medições foram feitas no mesmo lugar: o save na varanda da casa do Link, em Kokiri Forest, com 30 s parado e o Deck na tomada. O fps sai das linhas `present #` do log do Lichii. A cena é carregada sem ninguém tocar no aparelho, por um controle virtual (uinput) e uma fila de rodadas (`series.sh`) que roda sozinha no Deck como serviço do systemd.

## Parte 1: Steam Deck LCD

### Linha de base

| Tela | fps | Observação |
|---|---|---|
| Menu de título | 9,8 | GPU 99% a 1600 MHz |
| Jogo, imagem de fábrica | 10,2 a 11,6 | GPU 50 a 80% a 1040 MHz |

### O que não mexeu no número

Medido no mesmo ponto, com ruído de ±0,7 fps:

- GPU fixa em 1600 MHz: 10,8 fps.
- CPU em desempenho (EPP `performance`, piso em 3,5 GHz): 10,5 a 12,6 fps.
- `gamemoderun`: 10,3 fps. No Deck ele nem troca o governor.
- Threads da GPU presas em núcleos físicos separados: 11,8 fps.
- Add-on "Dynamic FPS" desligado: 11,8 fps.
- Thread única de GPU (`EDEN_NX2_SPLIT=0`): 10,7 fps.
- Seis patches de sincronização entre as threads da GPU (sem drenar semáforos, sem esperar escritor, leitura por página, evento pelo syncpoint do guest, sem leitura reativa): 9,4 a 11,7 fps.

### O diagnóstico

Com a thread de GPU numa só, o perfil fecha a conta do quadro de ~93 ms:

| Parte | ms por quadro |
|---|---|
| CPU da thread de GPU do emulador | 47,7 |
| Espera pela GPU real (`Finish` e listas de comando geradas por compute) | 41,6 |
| Lógica do jogo (guest) | ~0 (o jogo está sempre adiantado) |

A GPU sozinha ocupa ~58 ms por quadro a 1040 MHz. Mesmo com CPU e GPU totalmente sobrepostas, o teto é ~17 fps. Trinta fps com a imagem de fábrica não cabem neste APU de 15 W. A comunidade chega ao mesmo teto: o melhor resultado público no Deck é 14 a 18 fps, com o anti-aliasing do jogo desligado.

### O que o fork ganhou

- **Patches por variável** em `deck/patches/` (até o 0024) e uma série portada para o HEAD do upstream em `deck/patches-head/`.
- **`EDEN_NX2_ASTC_UNEVEN_RGBA8=0`**: as texturas ASTC de mips desalinhados passam a ser recomprimidas em BC1/BC3 em vez de ficar em RGBA8. Sem a chave, o carregamento do save estourava a memória e o earlyoom do SteamOS matava o Lichii em quase toda rodada. Com ela, as duas rodadas carregaram.
- **Correções de vazamento** no `READ_COPY` e na marcação de páginas lidas pela CPU: cresciam gigabytes por minuto no menu.
- **HEAD do upstream de 2026-11-08**: o menu foi a 30 fps (antes 9,8) e o boot caiu de ~7 min para ~2 min. No jogo, 9,3 a 9,6 fps.

## Parte 2: Mac com M2 Pro

O Lichii publica um build oficial para Apple Silicon e usa o MoltenVK (Vulkan sobre Metal).

### Primeiro contato

| Build | Menu | Jogo |
|---|---|---|
| Oficial build-117 | trava no vídeo de abertura (issue #237) | n/a |
| Local, fonte do PR #768 (compilado neste Mac, 13,5 min) | 57 a 60 fps, imagem de fábrica | 2 a 6 fps |

O menu roda 6 vezes mais rápido que no Deck. No jogo o Mac fica abaixo do Deck.

### Primeiras pistas

- O jogo usa task/mesh shaders, e o M2 Pro não tem `VK_EXT_mesh_shader` no MoltenVK, então o Lichii emula os draws de mesh. Desligar esses draws com o jogo aberto (`EDEN_NX2_NO_MESH_FILE`) não mudou o fps. Não é o gargalo.
- Metal System Trace de 4 s: a GPU fica ocupada 96 a 97% do tempo, ~180 ms por quadro. Na mesma cena, a GPU do Deck, ~4 vezes mais fraca, gasta ~58 ms. Os maiores custos:
  - um encoder de compute de ~58 ms, duas vezes por quadro;
  - ~195 render passes por quadro a ~0,46 ms cada.
- O bypass da rede de anti-aliasing (`oot_anti_aliasing=1`) não mudou nada: 6,0 fps. O compute de 58 ms é outra coisa.
- Na CPU, o guest roda no Dynarmic e não no NCE, e o `sample` mostra o caminho lento de memória (`DynarmicCallbacks64::MemoryRead`, `ExclusiveMonitor::CheckAndClear`).

### Meta

40 a 50 fps no jogo, com a imagem de fábrica. A conta permite: a GPU do M2 Pro deveria fazer a cena em 15 a 20 ms, e os núcleos de CPU são ~2 vezes mais rápidos que os do Deck. Falta tirar o caminho Vulkan → MoltenVK → Metal da ineficiência de ~12 vezes e a CPU do caminho lento.

### O que os contadores da GPU mostraram

Um novo Metal System Trace, agora com o bypass do anti-aliasing ligado, contou outra história.

- **O compute de 58 ms era mesmo a rede de anti-aliasing.** Com o bypass ele sumiu do trace. O quadro continuou em ~180 ms porque a rede não era o único problema.
- **A GPU parece cheia, mas fica ociosa por dentro.** Por quadro há ~310 encoders de render (~120 ms de fragment) e ~500 de compute. Os contadores mostram ocupação de fragment em ~10% e ALU em ~7%. O compilador do Metal registra spill de registradores em quase todo encoder.
- **Causa provável na tradução de shaders.** O yuzu decora toda multiplicação e soma de ponto flutuante com `NoContraction`, para impedir FMA e manter o resultado exato. O SPIRV-Cross traduz cada uma dessas operações para uma função `spvFMul`/`spvFAdd` marcada `[[clang::optnone]]`, que o compilador do Metal não consegue inlinar. No dump de shaders são 6.284 chamadas a `spvFMul` em 24 shaders.
- **Um compute extra depois de cada render pass.** O MoltenVK acumula o resultado das occlusion queries com um compute depois de cada render pass, ~300 vezes por quadro, porque o contador ZPass do Ocarina fica sempre ligado.
- **Não há NCE no macOS.** O código nativo do guest só existe para Android e Linux (`CMakeLists.txt:324`). No Mac o guest roda no Dynarmic.

Duas correções implementadas, cada uma atrás de uma chave:

- `EDEN_MAC_FAST_FP=1`: tira o `NoContraction` no caminho do MoltenVK, para os shaders voltarem a ser código inlinável.
- `EDEN_MAC_NO_OCCLUSION=<n>`: reduz o acúmulo de occlusion queries depois de cada render pass.

### Primeiro resultado: `EDEN_MAC_FAST_FP=1`

| Medida | Antes | Com `EDEN_MAC_FAST_FP=1` |
|---|---|---|
| Jogo, varanda em Kokiri | 5,9 fps | ~18,5 fps |
| Tempo de quadro (vi2) | 170 a 178 ms | 54 ms |
| Barra de status do Lichii | 6 fps | 19 fps / 52 ms |

O ganho foi de três vezes, mexendo só na decoração de ponto flutuante. Os 808 shaders recompilados ficaram sem nenhuma chamada a `spvFMul`/`spvFAdd`. Na primeira abertura depois da mudança o Lichii recompila ~800 pipelines e o menu demora 326 s para aparecer, contra 85 s antes. Isso acontece uma vez só; nas aberturas seguintes os pipelines saem do cache.

Uma nota de método: nessa rodada o script de medição imprimiu 1,4 fps por um bug. Ele pegava o instante da última linha do log, que às vezes vem colada a outra linha escrita ao mesmo tempo. Corrigido para usar o instante da mesma linha do contador de quadros.

### Segunda medição: occlusion queries e um trace novo

O corte das occlusion queries por cima do `FAST_FP` (`EDEN_MAC_NO_OCCLUSION=1000`) deu 18,6 fps, com a GPU em 85%. Sem ele eram ~18,5. A acumulação de queries do MoltenVK não era gargalo, e a chave sai do código.

O trace novo com `FAST_FP` mostra para onde foi o tempo de GPU:

| Canal | Antes (ms/quadro) | Com `FAST_FP` (ms/quadro) |
|---|---|---|
| Fragment | ~120 | ~22 |
| Compute | ~35 | ~16 |
| Vertex | ~23 | ~8 |

Restam ~47 ms de GPU espalhados por ~1.050 encoders por quadro: ~350 de render, ~350 de compute e o resto em cópias. A GPU fica em ~85%. Agora o custo não está concentrado num lugar só; ele está no volume de encoders.

> **Correção:** todas as medições do Mac desde "Primeiro resultado" rodaram com o bypass da rede de anti-aliasing ligado (`oot_anti_aliasing=1`). Não são números com a imagem de fábrica. A medição com a rede ligada vem a seguir.

### Terceira medição: argument buffers do Metal

`MVK_CONFIG_USE_METAL_ARGUMENT_BUFFERS=0` faz o MoltenVK amarrar os recursos direto no encoder, sem argument buffers. Junto com o `FAST_FP` o jogo foi a **23,4 fps** (vi2: 43 a 46 ms por quadro), contra 18,5 só com o `FAST_FP`. A GPU caiu para 79%.

Como diagnóstico, com os draws de mesh desligados (`EDEN_NX2_NO_MESH=1`, que deixa a imagem errada), o quadro fica em 42 ms. A emulação de mesh custa ~12 ms por quadro:

- ~8 ms de compute: três dispatches pequenos com barreira em cada draw, ~66 draws por quadro;
- o resto em ~120 render passes extras por quadro, de entrar e sair do pass para cada draw emulado.

Na CPU, as threads do guest ficam 60 a 80% ocupadas, com ~10% em `ExclusiveMonitor::CheckAndClear` e ~10% em `swtch_pri` (yield). A thread de GPU fica em ~48%.

> **Correção 2:** os 23,4 fps de `MVK_CONFIG_USE_METAL_ARGUMENT_BUFFERS=0` não valem. Sem argument buffers, 57 pipelines estouram o limite de 31 buffers por estágio do Metal e a imagem sai estourada. Era um quadro mais barato porque estava errado.

### A imagem de fábrica

Com a rede de anti-aliasing do jogo ligada (`oot_anti_aliasing=Game`) e `EDEN_MAC_FAST_FP=1`, o jogo fica em **9,6 fps** (vi2: ~104 ms por quadro), com a GPU em 92 a 93%. Repeti duas vezes e deu o mesmo número.

Agora a rede domina o quadro: um único encoder de ~53 ms ("Dispatch & FillBuffer & Dispatch"). O compilador do Metal registra 7.744 bytes de spill de registradores por thread nesse encoder. A causa:

- A rede usa matrizes em tensor cores, e o Lichii emula o HMMA em software com shuffles de subgrupo e `fma` (`emit_spirv_tensor.cpp:232` em diante).
- O SPIRV-Cross marca a função de HMMA como `always_inline`. Ela é chamada 193 vezes num kernel de 957 KB de MSL, e a pressão de registradores estoura.
- O M2 não expõe cooperative matrix pelo MoltenVK.

Não é coisa de flag. A correção de verdade é um caminho nativo com `simdgroup_matrix` do Metal, ou um HMMA emitido com menos pressão de registradores (laço em vez de 193 cópias inline).

### Fábrica: antes e depois do `FAST_FP`

| Mac, Kokiri, imagem de fábrica | fps | GPU |
|---|---|---|
| Sem correção (FAC0) | 4,9 | 96% |
| `EDEN_MAC_FAST_FP=1` (FAC-FFP) | 9,6 | 92–93% |

Com a imagem de fábrica o `FAST_FP` também dobra o fps.

Tentativa descartada: um `simdgroup_barrier(mem_none)` no início de cada HMMA em software (`EDEN_MAC_HMMA_FENCE`), para o compilador não intercalar os shuffles dos HMMAs inlinados. Deu 9,3 fps e o spill subiu de 7.744 para 7.840 B/thread. Revertido.

O Lichii já emite o HMMA como `OpFunction` com `DontInline`. Quem descarta essa marca é o SPIRV-Cross dentro do MoltenVK: ele gera `static inline __attribute__((always_inline))`. Próximo passo: compilar o MoltenVK localmente com o SPIRV-Cross honrando `DontInline`, e medir o spill e o fps.

### O MoltenVK que o Lichii usa de verdade

Para o MoltenVK local, a primeira rodada de controle (fábrica + `FAST_FP`, MoltenVK 1.4.2 oficial compilado aqui, carregado por `LIBVULKAN_PATH`) deu 7,3 fps (~132 ms), e não 9,6. O motivo: o Lichii nunca usou o MoltenVK do Homebrew. O `.app` traz o próprio `libMoltenVK`, que é o fork do Ryujinx, v1.4.1 (`cpmfile.json:172-176`, artefato `moltenvk@v1.4.1-ryujinx`). Todas as medições anteriores rodaram com esse fork, que aqui é ~30% mais rápido que o 1.4.2 oficial. A comparação do patch do SPIRV-Cross fica então contra essa base de 7,3 fps, com a mesma biblioteca. Se o patch ganhar, ele precisa ser portado para o fork do Ryujinx.

O patch: o SPIRV-Cross passa a guardar o `FunctionControl` no parser, e o `CompilerMSL` emite `static __attribute__((noinline))` quando a função tem `DontInline` e a opção está ligada (`MVK_CONFIG_SPIRV_HONOR_DONTINLINE=1`).

### O SPIRV-Cross honrando `DontInline`: o spill some, o quadro piora

| Mac, fábrica + `FAST_FP`, MoltenVK 1.4.2 local | fps | GPU |
|---|---|---|
| Sem o patch (FAC-MVKL) | 7,3 | 82% |
| Com `MVK_CONFIG_SPIRV_HONOR_DONTINLINE=1` (FAC-NOINL) | 4,6 | 73% |

O compilador do Metal aceitou o `noinline` e a imagem saiu correta. O spill dos maiores dispatches da rede caiu de 7.744 para 32 bytes por thread. Mesmo assim o quadro piorou: os encoders de compute passaram de ~350 para ~1.300 a 1.480 por quadro, com 41 a 54 ms de compute e ~35 ms de fragment.

Inferência: o patch vale para toda função marcada `DontInline` no Lichii, não só para o HMMA, e o custo das chamadas engole o ganho do spill. O próximo teste é restringir o `noinline` ao HMMA.

Uma armadilha de método: com o cache de pipelines quente, o MoltenVK reaproveita o MSL antigo e o patch não entra. É preciso medir com o cache frio.

### Onde paramos no Mac

| Mac, Kokiri | Sem correção | `EDEN_MAC_FAST_FP=1` |
|---|---|---|
| Imagem de fábrica | 4,9 fps | 9,6 fps (~104 ms) |
| Bypass do anti-aliasing | 5,9 fps | 19,4 fps (~52 ms, GPU 88%) |

A correção está em `mac/patches/0001-mac-fast-fp.patch`, e o relatório completo do perfil, com 11 rodadas, em [`mac-gpu-report.md`](mac-gpu-report.md). Uma ressalva para comparar com o Deck: o Deck roda em resolução 3/4, ou seja, com ~56% dos pixels do Mac.

A meta de 40 a 50 fps com a imagem de fábrica ainda não foi atingida. O que falta, em ordem de ganho:

1. **Tirar a rede de anti-aliasing do HMMA em software** (~53 ms por quadro). Ou um SPIRV-Cross que respeite o `DontInline`, compilado num MoltenVK local, ou um compute Metal próprio com `simdgroup_matrix` para os programas da rede.
2. **Juntar os computes da emulação de mesh** num prólogo antes do render pass (até ~12 ms por quadro).
3. **CPU do guest no Dynarmic**: não há NCE no macOS (`CMakeLists.txt:324`), e há ~10% em `ExclusiveMonitor::CheckAndClear`.
