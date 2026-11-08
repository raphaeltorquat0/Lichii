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

## Adendo: MoltenVK local (pendente)

- O fonte do MoltenVK v1.4.2 está clonado em `~/Games/lichii-mac/mvk` (SPIRV-Cross na revisão 6c09849f). Nada foi
  compilado: o `./fetchDependencies --macos` foi bloqueado pela política de permissões (baixa e compila código
  externo). Precisa de liberação do usuário.
- Carregar o MoltenVK local não precisa de ICD: o Lichii lê `LIBVULKAN_PATH` antes da cópia do bundle
  (`src/video_core/vulkan_common/vulkan_library.cpp:162-168`).
- Plano: no `CompilerMSL`, emitir `noinline` quando a OpFunction tiver `DontInline`, atrás de uma variável
  `MVK_CONFIG_*`. Depois a rodada FAC-NOINL: spill no encoder da rede, fps e print contra o FAC-FFP.

## Adendo 2: MoltenVK local e noinline (build feito pelo usuário)

- FATO: o Lichii nunca usou o MoltenVK do Homebrew. O bundle traz o fork do Ryujinx v1.4.1
  (`cpmfile.json:172-176`, `moltenvk@v1.4.1-ryujinx`; o stderr diz "MoltenVK version 1.4.1"). Todas as rodadas
  anteriores usaram esse fork.
- FAC-MVKL (fábrica + FAST_FP, MoltenVK 1.4.2 oficial local sem patch, `LIBVULKAN_PATH`): 7,3 fps, GPU 82%,
  vi2 ~132 ms. O fork do Ryujinx é ~30% mais rápido aqui (9,6 fps).
- Patches:
  - `patches/0002-spirv-cross-msl-honor-dontinline.patch`: o SPIRV-Cross guarda o FunctionControl e emite
    `static __attribute__((noinline))` quando há DontInline e a opção estiver ligada.
  - `patches/0003-moltenvk-env-honor-dontinline.patch`: liga a opção por `MVK_CONFIG_SPIRV_HONOR_DONTINLINE=1`.
  - Com cache de pipelines quente o MSL antigo é reaproveitado. A rodada precisa começar com o cache frio.
- FAC-NOINL (fábrica + FAST_FP + 1.4.2 com patch + variável, cache frio): 4,6 fps, GPU 73%, imagem correta.
  - O Metal aceitou o `noinline` sem nenhum mvk-error.
  - Spill: os maiores dispatches da rede caíram de 7.744 B para 32 B por thread. O maior spill restante no trace é
    de 1.808 B.
  - Mas o quadro piorou: ~1.300–1.480 compute encoders por quadro (eram ~350), compute 41–54 ms e fragment
    ~35 ms por quadro.
  - INFERÊNCIA: o `noinline` vale para toda função DontInline do Lichii, não só o HMMA, e o custo de chamada
    mais a fragmentação anulam o ganho do spill.
  - Falta o controle FAC-MVKL2 (mesma lib sem a variável): rodar o Lichii com a dylib local foi bloqueado pela
    política de permissões nesta etapa.
- Estado deixado: Lichii fechado; cache de pipelines do fork 1.4.1 restaurado (os de 1.4.2 estão em
  `backup/shader-cache/*.mvk142.bin` e `*.noinl.bin`); `qt-config.ini` em fábrica (`oot_anti_aliasing=0`); o
  original com o bypass está em `backup/qt-config.ini.bypass`.

## Adendo: emulação de mesh em lote antes do render pass (`EDEN_MAC_MESH_BATCH=1`)

Worktree `~/Games/lichii-mac/src-mesh` (pr768 9c54010 + patch 0001), build próprio em `build/release`.
Patch: `patches/0004-mac-mesh-batch.patch` (aplica por cima do 0001). Só liga com MoltenVK e a variável; sem ela o
caminho é o de antes.

### O que mudou

- `vk_scheduler.h` / `vk_scheduler.cpp`: o pass ativo ganha um **prólogo**. `BeginRenderPassImpl` guarda o comando
  anterior ao begin (`vk_scheduler.cpp:335-339`). `RecordInRenderPassPrologue` (`vk_scheduler.h:583-597`) liga o
  comando novo na lista do chunk **antes** do begin (`CommandChunk::RecordAfter`, `vk_scheduler.h:394-420`).
  Na primeira vez, `OpenRenderPassPrologue` (`vk_scheduler.cpp:462-510`) põe duas barreiras em volta do lote: uma de
  tudo que veio antes (gráfico/compute/cópia → compute) e uma do lote para o pass (compute → vertex/indireto).
  Resultado no comando: `[antes] [barreira] [mesh 1] [mesh 2] ... [barreira] [begin] [draws...]`.
- `vk_graphics_pipeline.cpp:858-1007`: o dispatch do mesh tenta o prólogo; se não der, cai no caminho antigo (sai do
  pass). Em lote, o dispatch pula a barreira de entrada e a de saída (o prólogo faz uma de cada para todos); as
  barreiras internas task → distribuição → mesh ficam.
- `mesh_descriptor_set` deixou de ser membro do pipeline: virou um `shared_ptr` por draw
  (`vk_graphics_pipeline.cpp:856`, bind em `:1089-1091`). Com lote, dois draws do mesmo pipeline no mesmo pass
  sobrescreveriam o membro antes de o primeiro draw ler.

Quando cai no caminho antigo (corretude), FATO pelo código:

- o pass que ficaria não é o mesmo (`RenderPass`/`Handle`/área diferentes, ou clear adiado), como no `RequestRenderpass`;
- o begin do pass já foi para o worker (o chunk encheu ou foi despachado: `DispatchWork` fecha o prólogo, `:307-308`);
- um draw anterior do mesmo pass escreveu storage (`NoteRenderPassStores`) ou transform feedback
  (`NoteRenderPassWrites`, `vk_graphics_pipeline.cpp:1008-1010`): o dispatch poderia ler essa escrita;
- o draw amostra um render target do próprio pass (`mesh_samples_render_target`, `vk_graphics_pipeline.cpp:800-802`);
- o pipeline ainda não terminou de compilar (o lote não espera `build->Wait`).

### Medições (imagem de fábrica, `oot_anti_aliasing=0`, `EDEN_MAC_FAST_FP=1`, mesma build)

| Tag | Mudança | fps | GPU% | Render passes/s | Passes/quadro | Fins de pass no mesh (`vk_graphics_pipeline.cpp:997`) |
|---|---|---|---|---|---|---|
| MESH0 | sem a chave | 9,2 | 92 | — | — | — |
| MESH1 | `EDEN_MAC_MESH_BATCH=1` | 9,7 | 90 | — | — | — |
| MESH0P | sem a chave + `EDEN_NX2_GPU_PROFILE=1` | 9,6 | 92 | ~2.140 | ~223 | ~463/s (~48/quadro) |
| MESH1P | chave + `EDEN_NX2_GPU_PROFILE=1` | 9,4 | 90 | ~1.705 | ~181 | ~42/s (~4/quadro) |

- FATO: o lote tira ~42 render passes por quadro (~19%): os fins de pass do mesh caem ~91%. Os ~4 restantes por quadro
  caem no caminho antigo por uma das condições acima (não separei qual).
- FATO: fps não mudou além do ruído (9,2/9,6 sem a chave, 9,7/9,4 com ela; vi2 ~107 ms vs ~103 ms). Na imagem de
  fábrica a rede de AA (~53 ms) domina o quadro, e a GPU segue em 90–92%.
- FATO: imagem igual. Prints `MESH0-game.png` e `MESH1-game.png`: mesma cena, folhagem, cogumelos e grama no lugar; só
  a fada e os vaga-lumes (animados) diferem.
- INFERÊNCIA: os 120 passes extras do relatório eram com bypass; aqui (fábrica) a conta é ~48 por quadro. O ganho de
  load/store por pass removido (estimado em ~0,05 ms cada no fragment, seção 2) daria ~2 ms por quadro, abaixo do
  ruído de uma rodada de 40 s a ~100 ms por quadro. Com o bypass (quadro de ~54 ms) deve aparecer melhor. Não medi
  porque a configuração não podia mudar.
- INFERÊNCIA: o resto dos ~12 ms do NOMESH não é quebra de pass. São os próprios dispatches (3 por draw, com 2
  barreiras internas cada) e a geometria expandida. O próximo passo seria agrupar por etapa (todas as tasks, uma
  barreira, todas as distribuições, uma barreira, todos os meshes).
- Risco aceito (INFERÊNCIA): o push constant do dispatch em lote roda antes dos draws anteriores do pass. O
  `down_factor` que ele escreve é o mesmo valor (mesmo framebuffer, mesmo `is_rescaling`). Os demais push constants são
  reescritos por draw.
- Não testado: um unitário do `RecordAfter`, já que o build não tem `YUZU_TESTS`. A validação foi a imagem e a contagem
  de passes. Também não testei com bypass. No Deck não se aplica: a chave só liga com MoltenVK.

Artefatos: `MESH{0,1}-game.png`, `MESH{0,1}P-eden_log.txt` (contadores do scheduler), `MESH*-stderr.log`.

## Adendo 3: rede de AA em MSL direto (EDEN_MAC_NATIVE_NET)

**Implementação:** `patches/0005-mac-native-net-msl.patch`, aplicado sobre o 0001. Worktree em `src-net`.

- `vk_shader_util.cpp`: `MacReplacementMsl` lê `$EDEN_MAC_NATIVE_NET/<FNV-1a 64 do SPIR-V>.metal`, só no
  MoltenVK. Para programas com `TensorMulAdd` que não têm arquivo, loga o hash.
- `BuildMslShader`: cria o módulo pelo caminho MSL direto do MoltenVK (magic `0x19960412`) e passa o workgroup
  size por `vkSetWorkgroupSizeMVK`, obtido com dlsym na lib do bundle.
- `vk_pipeline_cache.cpp`: usa esse módulo no lugar do SPIR-V quando há arquivo.
- `vk_compute_pipeline.*`: `pName` passa a ser `main0` para esses módulos.
- Sem a variável, nada muda.

**Gerador:** `net-tools/make_msl.py` (cópia em `patches/0005-mac-native-net-make_msl.py`). Ele parte do dump do
MoltenVK (`MVK_CONFIG_SHADER_DUMP_DIR`, fábrica + FAST_FP) e escreve os 11 kernels com HMMA:

- `noinline`: o mesmo MSL do MoltenVK, com o HMMA em `noinline`. A matemática é idêntica.
- `simdgroup`: o HMMA vira 4 `simdgroup_multiply_accumulate` 8x8x8 em float, com scratch de 2 KB por
  simdgroup em threadgroup e arredondamento para f16 depois de cada grupo de K=8. Sem validação numérica
  de buffer; só o print foi comparado.

| Tag | Config AA | Mudança | fps | GPU% | Observação |
|---|---|---|---|---|---|
| NET-NOINL | fábrica | FAST_FP + `EDEN_MAC_NATIVE_NET=net-msl/noinline` | 17,2 | 86 | vi2 ~60 ms. 10 programas trocados, nenhum mvk-error. Imagem igual à do FAC-FFP |
| NET-SG | fábrica | FAST_FP + `EDEN_MAC_NATIVE_NET=net-msl/simdgroup` | 17,0 | 86 | vi2 ~59 ms. Rede ~6,7 ms por quadro (trace), spill 0–32 B. Imagem igual a olho |

Leitura:

- Na imagem de fábrica: 4,9 (FAC0) → 9,6 (FAST_FP) → **17,2 fps** (FAST_FP + rede noinline).
- A rede caiu de ~53 ms para ~7–12 ms por quadro.
- O quadro (~59 ms) agora fica perto do caso com o bypass (~52 ms). O que sobra é o mesmo dos adendos
  anteriores: emulação de mesh (~12 ms), ~1.000 encoders por quadro, e a CPU.
- O simdgroup não ganhou fps sobre o noinline. Recomendo o noinline, que tem matemática idêntica.

**Limitação:** os .metal valem para o SPIR-V exato desta build com o 0001. Mudou o emissor ou o jogo, o hash
muda; o Lichii volta para o SPIR-V e loga o hash novo. Aí é preciso regenerar com o `make_msl.py` a partir de
um dump novo.

## Adendo 4: build combinado, sincronização CPU↔GPU e READ_COPY

Todas as rodadas abaixo usam a imagem de fábrica e o app `src-net` (0001 + 0004 + 0005 + 0006).

| Tag | Mudança | fps | GPU% | RSS | Observação |
|---|---|---|---|---|---|
| COMB | FAST_FP + NATIVE_NET noinline + `EDEN_MAC_MESH_BATCH=1` | 17,5 | 85 | 1.125 MiB | MESH_BATCH dá +0,3 sobre o NET-NOINL |
| COMB-NOREACT | COMB + `EDEN_NX2_NO_REACTIVE=1` (diagnóstico, pode ler dado velho) | 18,5 | 88 | 1.402 MiB | teto de eliminar as leituras reativas: ~+1 fps |
| COMB-RC | COMB + `EDEN_NX2_READ_COPY=1` (porte do 0006) | 16,9 | 87 | 1.286 MiB | sem ganho; as leituras seguem em ~64 por segundo |
| COMB-PROF | COMB + `EDEN_NX2_GPU_PROFILE=1` | 16,1 | 81 | 1.114 MiB | contabilidade abaixo |

**Trace do COMB, por quadro (~57 ms):**

- A GPU fica ocupada só 66% do tempo: ~37,6 ms de trabalho e ~20 ms ociosa.
- Trabalho:
  - fragment 15,4 ms (157 render passes);
  - dispatches 11,1 ms (40);
  - rede 7,0 ms;
  - vertex 5,7 ms;
  - cópias 2,1 ms (132 encoders).
- CPU (`sample`): nenhuma thread saturada.
  - Cores do guest em 54–64%, com ~8% em `ExclusiveMonitor::CheckAndClear` e ~10% em yield.
  - Thread de GPU front ~51%, back ~54%.

**COMB-PROF, por quadro (62 ms, 16,1 fps; valores por segundo do `nx2_profile` divididos por 16,1):**

- Front (thread GPU):
  - split drain 20,0 ms;
  - list GPU wait 15,8 ms (5,2 listas escritas pela GPU por quadro, ~3,0 ms cada);
  - macro 12,2 ms;
  - writer wait 8,4 ms;
  - resume pending 4,0 ms;
  - pending sleep 0,1 ms.
- Esperas da front pela back, por local:
  - macro dma read 11,9 ms (11.320 leituras DMA de macro por segundo, macro 0xe2c);
  - list writers 8,4 ms;
  - acquire 7,1 ms.
- Back:
  - **split wait (faminta) 25,0 ms**;
  - method 7,2 ms, draw 6,1 ms, bind stage buffers 2,5 ms;
  - "other" 12,5 ms.
  - Nenhum download de buffer (0 KiB): as leituras do guest não passam mais por download grande.

Leitura:

- FATO: no Mac o quadro de fábrica não é mais limitado pela GPU nem por uma thread de CPU.
- Ele é limitado pelo lock-step front↔back e pelas esperas da front pela GPU nas listas de comando escritas
  por compute (list GPU wait ~16 ms por quadro). É o mesmo quadro A2/A3 de
  `/tmp/oot-analysis/research/00-overlap.md` no Deck.
- As leituras da CPU (A1), que no Deck pesavam ~20 ms, aqui valem só ~4 ms (teto do NOREACT).

## Onde paramos / próximo passo (2026-11-08)

**Estado:**

- Worktree `~/Games/lichii-mac/src-net` (base 9c54010) com:
  - 0001 (`EDEN_MAC_FAST_FP`);
  - 0004 (`EDEN_MAC_MESH_BATCH`; o arquivo válido é `patches/0004-mac-mesh-batch.git.patch`, porque o
    `0004-mac-mesh-batch.patch` original é saída do rtk e não aplica);
  - 0005 (`EDEN_MAC_NATIVE_NET` + `net-msl/noinline`);
  - 0006 (porte do READ_COPY: 0005/0009/0010 de `deck/patches-head`, aplicados com `git apply -C1`).
- Compilado em `src-net/build/release/bin/lichii.app`.
- Melhor configuração medida: COMB = 17,5 fps na fábrica (FAC0 era 4,9).
- Config deixada em fábrica (`oot_anti_aliasing=0`); o original com o bypass está em
  `backup/qt-config.ini.bypass`. Lichii fechado, lock solto.

**Próximo passo, maior item medido (~36 ms por quadro de espera na front):**

1. Portar de `/tmp/lichii-fork/deck/patches-head/` o 0007-dep-marking, o 0008-dep-queue e o 0012-dep-routing
   (filas de dependência que tiram as listas escritas pela GPU da fila principal).
2. Portar o 0011-lockstep-knobs (`WRITER_WAIT=0`, `MACRO_DMA_NODRAIN`), medindo cada um como COMB+X
   com print e RSS.
3. Depois: os ~1.000 encoders por quadro e a CPU do guest (exclusivos do Dynarmic).

**Para retomar:**
```
cd ~/Games/lichii-mac/src-net && CCACHE_DIR=~/Games/lichii-mac/.ccache CCACHE_BASEDIR=~/Games/lichii-mac \
  cmake --build build/release --target yuzu --parallel 6 && codesign --force --deep --sign - build/release/bin/lichii.app
until mkdir /tmp/lichii-mac.lock 2>/dev/null; do sleep 60; done
~/Games/lichii-mac/run-save.sh ~/Games/lichii-mac/src-net/build/release/bin/lichii.app COMB \
  EDEN_MAC_FAST_FP=1 EDEN_MAC_NATIVE_NET=$HOME/Games/lichii-mac/net-msl/noinline EDEN_MAC_MESH_BATCH=1
pkill -x lichii; rmdir /tmp/lichii-mac.lock
```
