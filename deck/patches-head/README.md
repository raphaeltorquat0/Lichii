# Patches do Deck rebaseados sobre o upstream `zortos293/Lichii` nx2 `bf48fe9` (2026-11-08)

Base: `bf48fe9` ("gpu_thread: reset pending-channel poll timer…", #621), que já contém #818/#817
(render targets novos sem upload stale), #752 (lista escrita pela GPU suspende o canal), #296, #815.
Os 15 patches aplicam em sequência (`git apply --check` limpo um a um sobre `bf48fe9`, verificado em
worktree novo) e reproduzem a árvore de `/tmp/oot-analysis/wt-head` (HEAD `2b37009`, 29 arquivos,
+1215/−65). **Não compilado**: a revisão foi textual; CI com `ref=bf48fe9` + este diretório.

| Novo | Origem (fork) | Porte |
|---|---|---|
| 0001-oot-bypass-heavy-dispatch-log-output-pass | 0001 + 0002 + 0004 | `BypassOotNetwork` mudou de lugar (`kepler_compute.cpp:1055`) e a variável `state` virou `bypass`: três hunks reaplicados sobre o texto novo (um `state->` que sobrou foi corrigido). `oot_network.h` limpo. Um patch só porque os três tocam a mesma função. |
| 0002-hosttiming-timerslack | 0009 | limpo |
| 0003-tickframe-trace | 0011 | limpo |
| 0004-profile-fast-maxwell | 0013 | `g_trace_first_method_writes` → `trace_first_method_writes` em `maxwell_3d.cpp:58`; resto limpo |
| 0005-cpu-read-copy | 0014 | declaração em `buffer_cache_base.h` reaplicada após `Nx2NoteListSynced` (o `Nx2ReadListCopy` do #752 ganhou `pending_tick`); doc em `nx2_switches.txt` reposicionada (sem o bloco do PREFLUSH, que caiu) |
| 0006-list-writers-diag | 0015 | `Nx2CollectListCopies` reaplicado (contexto: enum `Nx2ListRead` ganhou `Pending`) |
| 0007-dep-marking | 0016 | só a doc em `nx2_switches.txt` reposicionada |
| 0008-dep-queue | 0017 | limpo |
| 0009-read-copy-bound | 0019 | limpo |
| 0010-read-mark-bound | 0022 | limpo |
| 0011-lockstep-knobs | 0012 | limpo. Nota: com #752 o `WaitForWriters` já é pulado ao retomar uma lista pendente; `WRITER_WAIT=0` continua valendo para a 1ª leitura e `MACRO_DMA_NODRAIN` é independente |
| 0012-dep-routing | 0020 | portado sobre #752: `Nx2ReadListCopy` mantém o retorno `Pending` e usa `Nx2CopyDone`/`Nx2WaitCopy`; `RasterizerVulkan::IsGpuTickDone` (novo no #752, `vk_rasterizer.cpp:2078`) aprende o `Nx2LaneTickBit` → `scheduler.IsDependencyFree`; declarações em `buffer_cache_base.h` reaplicadas. Os outros 8 arquivos limpos |
| 0013-gl-runtime-stubs | 0021 | limpo |
| 0014-834-fast-unswizzle | #834 patch 10 | limpo (`decoders.cpp`, `unswizzle_worker.cpp`), `EDEN_NX2_FAST_UNSWIZZLE=1` |
| 0015-834-rt-pool | #834 patch 06 | limpo (`vulkan_device.{h,cpp}`, `vulkan_memory_allocator.cpp`), `EDEN_NX2_RT_POOL=128`; convive com 0008 em `vulkan_device` |

## Caíram

| Fork | Motivo |
|---|---|
| 0005 run-ahead, 0007 preflush, 0010 event-guest | medidos sem ganho (±0,7 fps); 0007/0010 tocam código que o upstream mexeu |
| 0006 acquire-nodrain, 0008 doc | #231 (aberto) é a versão correta do mesmo; 0006 fica fora até ele mergear (inócuo, sem ganho isolado) |
| 0018 tc-gc-budget | substituído pelo 0023 por decisão do coordenador |
| **0023 tc-gc-critical** | **não portado**: 10 hunks rejeitados em 5 arquivos — o upstream reescreveu `texture_cache.h` em volta do coletor (`RunGarbageCollector`/`TickFrame`) e `vk_rasterizer` `TickFrame`. Precisa de porte manual por quem o escreveu, ou substituir por #823 (issue #739) quando mergear. Até lá, no Deck: `vram_usage_mode=Aggressive` ou `EDEN_NX2_TC_EXPECTED_MIB/CRITICAL_MIB` (ver `../05-07-notes.md`). |

## Observações do porte

- `#752` muda a semântica que o 0012/0020 assumiam: a frente não bloqueia mais em `Nx2ReadListCopy`;
  o canal fica pendente e `Step` consulta `IsGpuTickDone`. O 0012 (routing) só precisou que
  `IsGpuTickDone` conheça ticks da lane; as esperas na trás (`Nx2DownloadFromListCopies`) seguem
  por `Nx2WaitCopy`.
- Os três patches do bypass ficaram num só porque não há como separá-los sem reconstruir o
  histórico sobre a função reescrita; para desligar o bypass basta não aplicar o 0001.
- Worktrees: `/tmp/oot-analysis/wt-head` (commits do porte, um por patch de origem) e
  `/tmp/oot-analysis/wt-head-check` (verificação). Ref `upstream-nx2-head` em `/tmp/lichii-fork`
  aponta para `ad477ef` (upstream já avançou além de `bf48fe9`); o porte é sobre `bf48fe9`.
