# Uso: python3 -I make_msl.py <dump_dir> <out_dir> <modo>
# Para cada kernel de compute do dump do MoltenVK (MVK_CONFIG_SHADER_DUMP_DIR) que tenha o HMMA em
# software do Lichii (emit_spirv_tensor.cpp), grava <out_dir>/<FNV-1a 64 do .spv>.metal com o HMMA
# trocado. O vk_shader_util.cpp do Lichii procura o arquivo pelo mesmo hash (EDEN_MAC_NATIVE_NET).
#   noinline: só troca always_inline por noinline na função do HMMA (mesma matemática).
#   simdgroup: o HMMA vira 4 simdgroup_multiply_accumulate 8x8x8 em float, com os fragmentos
#              passando por um scratch em threadgroup por simdgroup; arredonda para f16 depois de
#              cada grupo de K=8, como o emulador.
import re, sys, pathlib, subprocess
dump, out, mode = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
HEAD = re.compile(r'^static inline __attribute__\(\(always_inline\)\)\n'
                  r'uint2 (_\d+)\(uint4 (_\d+), uint2 (_\d+), uint2 (_\d+), thread uint& gl_SubgroupInvocationID\)\n\{\n', re.M)
KERNEL_TAIL = 'uint gl_SubgroupInvocationID [[thread_index_in_simdgroup]])\n{\n'
SCRATCH = 512  # floats por simdgroup: A 16x16, B 16x8, C 16x8

def fnv(data):
    h = 0xcbf29ce484222325
    for b in data:
        h = ((h ^ b) * 0x100000001b3) & 0xffffffffffffffff
    return h

def function_end(msl, start):
    depth = 0
    for i in range(start, len(msl)):
        if msl[i] == '{': depth += 1
        elif msl[i] == '}':
            depth -= 1
            if depth == 0: return i + 1
    raise ValueError('função sem fim')

def simdgroup_body(name, a, b, c, k16):
    groups = 2 if k16 else 1
    lines = [f'''static __attribute__((noinline))
uint2 {name}_sg(uint4 {a}, uint2 {b}, uint2 {c}, thread uint& gl_SubgroupInvocationID, threadgroup float* s)
{{
    const uint l = gl_SubgroupInvocationID & 31u, g = l >> 2, t = (l & 3u) * 2u;
    simdgroup_barrier(mem_flags::mem_threadgroup);
    // Fragmentos m16n8k16 da NVIDIA: A[r][k] em s[r*16+k], B[k][n] em s[256+k*8+n], C[r][n] em s[384+r*8+n].
    const float2 a0 = float2(as_type<half2>({a}.x)), a1 = float2(as_type<half2>({a}.y));
    const float2 a2 = float2(as_type<half2>({a}.z)), a3 = float2(as_type<half2>({a}.w));
    const float2 b0 = float2(as_type<half2>({b}.x)), b1 = float2(as_type<half2>({b}.y));
    const float2 c0 = float2(as_type<half2>({c}.x)), c1 = float2(as_type<half2>({c}.y));
    s[g * 16 + t] = a0.x; s[g * 16 + t + 1] = a0.y;
    s[(g + 8) * 16 + t] = a1.x; s[(g + 8) * 16 + t + 1] = a1.y;
    s[g * 16 + t + 8] = {"a2.x" if k16 else "0.0f"}; s[g * 16 + t + 9] = {"a2.y" if k16 else "0.0f"};
    s[(g + 8) * 16 + t + 8] = {"a3.x" if k16 else "0.0f"}; s[(g + 8) * 16 + t + 9] = {"a3.y" if k16 else "0.0f"};
    s[256 + t * 8 + g] = b0.x; s[256 + (t + 1) * 8 + g] = b0.y;
    s[256 + (t + 8) * 8 + g] = {"b1.x" if k16 else "0.0f"}; s[256 + (t + 9) * 8 + g] = {"b1.y" if k16 else "0.0f"};
    s[384 + g * 8 + t] = c0.x; s[384 + g * 8 + t + 1] = c0.y;
    s[384 + (g + 8) * 8 + t] = c1.x; s[384 + (g + 8) * 8 + t + 1] = c1.y;
    simdgroup_barrier(mem_flags::mem_threadgroup);
    simdgroup_float8x8 ma, mb, mc;''']
    for k in range(groups):
        lines.append(f'''    simdgroup_load(mb, s + 256 + {k * 64}, 8);
    simdgroup_load(ma, s + {k * 8}, 16); simdgroup_load(mc, s + 384, 8);
    simdgroup_multiply_accumulate(mc, ma, mb, mc); simdgroup_store(mc, s + 384, 8);
    simdgroup_load(ma, s + {128 + k * 8}, 16); simdgroup_load(mc, s + 448, 8);
    simdgroup_multiply_accumulate(mc, ma, mb, mc); simdgroup_store(mc, s + 448, 8);
    simdgroup_barrier(mem_flags::mem_threadgroup);''')
        if k + 1 < groups:
            lines.append('''    // HMMA.F16 arredonda para f16 depois de cada grupo de K=8.
    for (uint i = l; i < 128u; i += 32u) { s[384 + i] = float(half(s[384 + i])); }
    simdgroup_barrier(mem_flags::mem_threadgroup);''')
    lines.append(f'''    return uint2(as_type<uint>(half2(float2(s[384 + g * 8 + t], s[384 + g * 8 + t + 1]))),
                 as_type<uint>(half2(float2(s[384 + (g + 8) * 8 + t], s[384 + (g + 8) * 8 + t + 1]))));
}}
#define {name}(A_, B_, C_, L_) {name}_sg(A_, B_, C_, L_, spvHmmaScratch + spvSimdgroupIndex * {SCRATCH}u)
''')
    return '\n'.join(lines)

def transform(msl, invocations):
    if mode == 'noinline':
        return re.subn(r'^static inline __attribute__\(\(always_inline\)\)\n(uint2 _\d+\(uint4 _\d+, uint2 _\d+, uint2 _\d+, thread uint& gl_SubgroupInvocationID\))$',
                       r'static __attribute__((noinline))\n\1', msl, flags=re.M)
    if mode != 'simdgroup':
        raise SystemExit('modo desconhecido: ' + mode)
    count = 0
    while (m := HEAD.search(msl)):
        end = function_end(msl, m.end() - 2)
        body = msl[m.start():end]
        k16 = '< 2u)' in body  # a variante K=16 percorre os dois grupos num laço
        msl = msl[:m.start()] + simdgroup_body(m.group(1), m.group(2), m.group(3), m.group(4), k16) + msl[end:]
        count += 1
    if count:
        assert msl.count(KERNEL_TAIL) == 1
        msl = msl.replace(KERNEL_TAIL, KERNEL_TAIL[:-4] + ', uint spvSimdgroupIndex [[simdgroup_index_in_threadgroup]])\n{\n'
                          f'    threadgroup float spvHmmaScratch[{(invocations // 32) * SCRATCH}];\n')
    return msl, count

def invocations(spv_path):
    text = subprocess.run(['spirv-dis', str(spv_path)], capture_output=True, text=True).stdout
    x, y, z = map(int, re.search(r'LocalSize (\d+) (\d+) (\d+)', text).groups())
    return x * y * z

out.mkdir(parents=True, exist_ok=True)
for metal in sorted(dump.glob('shader-cs-*.metal')):
    msl = metal.read_text()
    if not HEAD.search(msl):
        continue
    spv_path = metal.with_suffix('.spv')
    new, n = transform(msl, invocations(spv_path))
    name = f'{fnv(spv_path.read_bytes()):016x}.metal'
    (out / name).write_text(new)
    print(metal.name, '->', name, 'hmma functions:', n)

# Auto-teste do FNV (mesmos valores do vk_shader_util.cpp).
assert fnv(b'') == 0xcbf29ce484222325 and fnv(b'a') == 0xaf63dc4c8601ec8c
