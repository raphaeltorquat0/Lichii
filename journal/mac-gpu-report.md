# Lichii no Mac (M2 Pro, MoltenVK 1.4.2): por que o Ocarina roda a 5–6 fps e o que mudou

Data: 2026-11-08. Fonte: `~/Games/lichii-mac/src` (PR #768, head 9c54010). Cena: save em Kokiri, varanda do Link.
Medição: `run-save.sh` (fps pelo contador de quadros do log, GPU% pelo ioreg) e, para confirmar, a linha
`nx2:vi2 last 600 presents: frame N us` do log e a barra de status do Lichii no print.

## Resumo

- **Causa principal (FATO):** o shader recompiler decora todo FMUL/FADD/FFMA do guest com `NoContraction`
  (`src/shader_recompiler/backend/spirv/emit_spirv_floating_point.cpp:14`). O SPIRV-Cross do MoltenVK traduz cada
  operação com essa decoração numa chamada a helpers `[[clang::optnone]] spvFMul/spvFAdd`. O compilador do Metal
  não consegue inlinar nem otimizar esses helpers. Resultado: spill de registradores em quase todo encoder,
  ocupação de fragment em ~10% e ALU em ~7%, com a GPU "96% ocupada".
- **Correção:** `EDEN_MAC_FAST_FP=1` (só com MoltenVK; o padrão não muda) emite as operações sem `NoContraction`.
  - Com o bypass da rede de AA: 5,9 → ~18,5 fps (~170 → ~54 ms por quadro).
  - Com a imagem de fábrica: 4,9 → 9,6 fps.
- **O que sobra na imagem de fábrica (FATO):** a rede de AA do jogo, ~53 ms por quadro num único encoder de
  compute. O compilador do Metal reporta 7.744 B de spill por thread nele. É o HMMA em software, inlinado
  193 vezes num kernel de 957 KB de MSL.
- **Não resolvido nesta sessão:** a meta de 40–50 fps com a imagem de fábrica.

## Tabela de rodadas

"Config AA" diz se a rodada usou o bypass da rede (`oot_anti_aliasing=1`, No sharpening) ou a imagem de fábrica
(`oot_anti_aliasing=0`, Game).

| Tag | Config AA | Mudança | fps | GPU% | Observação |
|---|---|---|---|---|---|
| MAC0 | bypass | nenhuma (base) | 5,9 | 96 | vi2 ~170–178 ms |
| FFP1 | bypass | `EDEN_MAC_FAST_FP=1` | ~18,5 (o script imprimiu 1,4) | 86 | vi2 54 ms, barra 19 FPS / 52 ms. O 1,4 era bug do script, corrigido (ver abaixo) |
| OCC1 | bypass | FAST_FP + `EDEN_MAC_NO_OCCLUSION=1000` | 18,6 | 85 | sem ganho, knob removido |
| DKB1 | bypass | FAST_FP + `EDEN_DKB_FRAME_TRACE=1` | 18,5 | 87 | gpu_frame_span ~50 ms, soma das submissões ~45 ms, ~45 submits por quadro |
| NOMESH | bypass | FAST_FP + `EDEN_NX2_NO_MESH=1` (diagnóstico, não é correção) | ~23,7 (vi2 42 ms) | 79 | teto: a emulação de mesh custa ~12 ms |
| ARG0 | bypass | FAST_FP + `MVK_CONFIG_USE_METAL_ARGUMENT_BUFFERS=0` | 23,4 | 79 | **inválida**: 57 pipelines falham (limite de 31 buffers) e a imagem sai estourada |
| FAC0 | fábrica | nenhuma (base) | 4,9 | 96 | |
| FAC-FFP | fábrica | `EDEN_MAC_FAST_FP=1` | 9,6 | 92 | vi2 ~104 ms |
| FACDUMP | fábrica | FAST_FP, cache de pipelines frio, dump de MSL | 9,6 | 93 | repete o FAC-FFP |
| FAC-FENCE | fábrica | FAST_FP + `simdgroup_barrier` em cada HMMA | 9,3 | 93 | spill 7.840 B, pior: revertido |
| BYP-FFP | bypass | `EDEN_MAC_FAST_FP=1`, binário final | 19,4 | 88 | confirmação com o binário dos patches |

Correção de medição (FATO): o `run-save.sh` pegava o instante da última linha do log, que às vezes vem colada a
outra linha (escrita concorrente). Agora usa o instante da mesma linha do contador de quadros. O original ficou
em `backup/run-save.sh.orig`.

## 1. O encoder de 58 ms

- FATO: no trace antigo (`/tmp/oot-mst.trace`) ele aparece como "vkCmdDispatch & vkCmdFillBuffer & vkCmdDispatch",
  ~1 por quadro, ~53–58 ms. No trace com o bypass ligado (`/tmp/oot-mst2.trace`) ele some. No trace de fábrica
  com FAST_FP (`/tmp/oot-mst5.trace`) ele volta: 36 em 3 s, 50–56 ms cada.
  - Conclusão: é a rede de AA do jogo (`src/video_core/oot_network.h`, programas `0x8475d671…`, `0x42593194…`,
    `0xda6df3e5…`). O FillBuffer no meio é o vkCmdFillBuffer que o MoltenVK implementa como compute.
- FATO: o encoder roda sozinho na GPU, sem nada concorrente, e o compilador reporta **7.744 bytes de spill por
  thread** (tabela `graphics-compiler-spill-events`).
- FATO: o M2 Pro não tem cooperative matrix pelo MoltenVK, então o HMMA do guest vai para a emulação por
  shuffle de subgrupo (`emit_spirv_tensor.cpp:232` em diante).
  - Essa função já é emitida como `OpFunction … DontInline` (`emit_spirv_tensor.cpp:264`).
  - O SPIRV-Cross ignora isso e gera `static inline __attribute__((always_inline))`.
  - O kernel maior (`/tmp/mvk-dump3/shader-cs-299e32ec2ef895ab.metal`) tem 193 chamadas inlinadas.
- INFERÊNCIA: o Deck também usa a emulação por shuffle, já que o RDNA2 não tem WMMA. Lá ela cabe nos 256 VGPRs
  por lane; no Apple GPU o compilador derrama. O limitador é latência de memória de spill, não ALU.
- Tentado e descartado: `simdgroup_barrier(mem_none)` no início de cada HMMA, como cerca de agendamento
  (FAC-FENCE). O spill não caiu e o fps caiu 0,3.

## 2. Os ~195–310 render passes por quadro

- FATO: antes da correção eram ~310 render encoders, ~500 compute encoders e ~300 encoders "Post-render-pass
  occlusion query accumulation" por quadro (Metal System Trace).
- Origem das quebras de pass:
  - **FATO:** cada draw de mesh emulado sai do render pass, roda 3 dispatches (task, distribuição `(1,1,1)`,
    mesh indireto) com barreira entre eles e volta ao pass (`vk_graphics_pipeline.cpp:854`). São ~66 draws por
    quadro.
    - Com `EDEN_NX2_NO_MESH=1` os render encoders caem de ~350 para ~230 por quadro, o compute de 16 para 8,5 ms
      e o fragment de 22 para 16,5 ms.
    - Ou seja, ~120 passes extras e ~12 ms por quadro são da tradução de mesh.
  - **FATO:** o MoltenVK fecha cada pass que teve query ativa com um compute de acumulação. O Lichii pausa e
    retoma o contador ZPass a cada pass (`vk_scheduler.cpp:889`), e o Ocarina deixa o contador sempre ligado.
  - **Medido:** remover as queries (OCC1) não mudou o fps. O custo é de encoder, não de GPU; o knob foi
    removido.
  - O resto (INFERÊNCIA): passes reais do jogo, mais quebras por cópia, clear e dispatch do jogo, que exigem sair
    do pass (`RequestOutsideRenderPassOperationContext`).
- Load/store na GPU de tiles:
  - INFERÊNCIA a partir do trace: os passes curtos custam ~0,05 ms cada no fragment.
  - Os 120 passes extras somam ~6 ms de fragment/vertex, junto com a geometria da folhagem.
  - O limitador de load/store por pass não é o principal. Os contadores de banda vêm zerados neste perfil, então
    isso não está medido diretamente.

## 3. Correção implementada

`patches/0001-mac-fast-fp-sem-nocontraction.patch` (git diff do worktree, sem commit). Arquivos:

- `src/shader_recompiler/profile.h:108`: `Profile::drop_no_contraction`.
- `src/shader_recompiler/backend/spirv/emit_spirv_floating_point.cpp:14`: não decora quando o flag está ligado.
- `src/video_core/renderer_vulkan/vk_pipeline_cache.cpp:585-590`: liga só se `device.IsMoltenVK()` e
  `EDEN_MAC_FAST_FP` estiver definido; registra no log.

Efeitos e custos:

- Efeito medido (trace com o bypass ligado): fragment ~120 → ~22 ms por quadro, compute ~35 → ~16 ms,
  vertex ~23 → ~8 ms.
- O MSL recompilado (808 shaders em `/tmp/mvk-dump2`) não tem nenhum `spvFMul/spvFAdd`.
- Imagem: o print do FFP1 bate com o do MAC0 (mesma cena; só a fada se mexe).
- Precisão (INFERÊNCIA): sem `NoContraction`, o Metal pode fundir `a*b+c` em FMA, que arredonda uma vez em vez
  de duas. A diferença fica no último bit. Não vi diferença visual.
- Custo único: a primeira abertura depois de ligar recompila ~800 pipelines (menu em 326 s em vez de ~85 s).

## Outros fatos levantados

- NCE: só existe para `ARCHITECTURE_arm64 AND (ANDROID OR LINUX)` (`CMakeLists.txt:324`). No macOS o guest roda
  em Dynarmic.
- `sample` com FAST_FP e sem mesh:
  - threads do guest 60–80% ocupadas, com ~10% em `Dynarmic::ExclusiveMonitor::CheckAndClear` e ~10% em
    `swtch_pri` (yield);
  - thread `GPU` ~48%, com `Maxwell3D::FrontRunMacro` e `GpuSplit::Drain` no topo.
  - Na faixa de 42–54 ms por quadro, CPU e GPU estão perto do limite ao mesmo tempo.
- Comparação com o Deck: o `deck-qt-config.ini:1428` usa `resolution_setup=2` (Res3_4X). O Mac usa Res1X.
  Os 58 ms do Deck são com 56% dos pixels do Mac. O Deck também usa EDS2, sem FXAA e filtro bilinear; o Mac usa
  FXAA e FSR3 na saída.
- Os contadores da GPU confirmam a GPU ao máximo de clock durante o jogo (gpu-performance-state "Maximum") e
  estado térmico nominal.

## Próximos passos, por ganho estimado (não implementados)

1. **Rede de AA nativa (~45 ms por quadro na fábrica).** Um pipeline de compute Metal próprio para os 2–4
   programas da rede, com `simdgroup_matrix<half,8,8>`, plugado no `KeplerCompute` como o bypass faz. Precisa
   reproduzir a matemática da rede e comparar a imagem com o FAC0. São dias de trabalho.
   - Alternativa: um SPIRV-Cross no MoltenVK que respeite `DontInline` (`noinline`). O spill deve cair; não está
     medido.
2. **Emulação de mesh (~12 ms por quadro, medido pelo NOMESH).**
   - Juntar os computes de mesh de um mesmo render pass num prólogo antes do pass, o que remove ~120 passes por
     quadro.
   - Agrupar os dispatches com uma barreira por etapa em vez de três por draw.
   - Exige trocar o `mesh_descriptor_set` membro (`vk_graphics_pipeline.h:343`) por um slot por draw.
3. **CPU:** exclusivos do Dynarmic e o caminho de drenagem do `GpuSplit`, para quando a GPU sair da frente.

Artefatos:

- Traces: `/tmp/oot-mst2.trace` (bypass, com contadores e shader profiler), `/tmp/oot-mst3.trace` (FAST_FP),
  `/tmp/oot-mst4.trace` (NOMESH), `/tmp/oot-mst5.trace` (fábrica + FAST_FP).
- Análises: `/tmp/mvkan/*.py`. Dumps de MSL: `/tmp/mvk-dump*`.
- Configuração: a de fábrica que usei está em `backup/qt-config.ini.factory`; a original (bypass) foi restaurada
  em `~/.config/eden/qt-config.ini`.
- Cache de pipelines: o original ficou em `backup/shader-cache/` e foi restaurado.
