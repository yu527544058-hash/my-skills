#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AHT 目标测算 —— 一次跑完配对检验、方差分解、外推检验、目标曲线。

用法:
  python3 aht.py DATA.csv --baseline 500 --baseline-is short
  python3 aht.py DATA.csv --baseline 500 --baseline-is mixed --q0 0.10
  python3 aht.py DATA.csv --baseline 500 --baseline-is short --q 0.20 --t-long 1800

列名自动识别（不区分大小写、忽略空格）；识别不到时用 --cols 按位置指定：
  worker / annotator / 标注员      标注员 ID  （可选，有则做配对分析）
  item / item id / case / 任务      Item ID    （可选，有则做配对分析）
  segment / size / count / 字数…    规模/复杂度变量（可选，缺了只是少一段诊断）
  aht / duration / 时长             处理时长（秒）

非数值的 AHT（deferred / In check / Not found / 空）自动剔除并单独统计。
"""
import argparse, csv, math, sys
from collections import defaultdict, OrderedDict

BAD = {'deferred', 'defered', 'in check', 'incheck', 'not found', 'notfound', 'n/a', 'na', '-', ''}
ALIAS = {
    'worker': ['worker', 'annotator', 'user', 'operator', 'agent', 'assignee', 'owner', 'reviewer',
               '标注员', '员工', '账号', '处理人', '操作人', '处理员', '客服', '审核员', '质检员', '作业员'],
    'item':   ['item', 'itemid', 'item id', 'case', 'caseid', 'case id', 'task', 'taskid', 'ticket',
               'ticketid', 'jobid', 'id', '任务', '任务id', '工单', '工单号', '单号', '案件', 'caseno'],
    # 规模/复杂度驱动变量：换项目时这里加你自己的列名
    'seg':    ['segment', 'segments', 'segmentnumber', 'segment number', 'seg', 'segnum',
               'size', 'count', 'length', 'complexity', 'volume', 'items', 'lines', 'pages',
               'segment数', '数量', '条数', '字数', '页数', '时长', '规模', '复杂度'],
    'aht':    ['aht', 'duration', 'time', 'handletime', 'handling time', '时长', '处理时长'],
}


# ---------- 基础统计（不依赖 numpy/scipy） ----------
def mean(v): return sum(v) / len(v)
def median(v):
    s = sorted(v); n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
def stdev(v):
    if len(v) < 2: return 0.0
    m = mean(v); return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))
def sem(v): return stdev(v) / math.sqrt(len(v)) if len(v) > 1 else 0.0
def pct(v, p):
    s = sorted(v); i = (len(s) - 1) * p; lo = int(i)
    return s[lo] + (s[min(lo + 1, len(s) - 1)] - s[lo]) * (i - lo)
def gmean(v): return math.exp(mean([math.log(x) for x in v]))

def ols(xs, ys):
    """返回 (截距, 斜率, R², 斜率标准误)"""
    n = len(xs)
    if n < 3: return (None,) * 4
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0: return (None,) * 4
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    ssr = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    sst = sum((y - my) ** 2 for y in ys)
    return a, b, (1 - ssr / sst if sst else 0.0), math.sqrt(ssr / (n - 2) / sxx)

def decompose(groups):
    """组间/组内 方差占比。groups: {key: [值...]}"""
    flat = [x for v in groups.values() for x in v]
    if len(flat) < 2: return None, None
    gm = mean(flat)
    b = sum(len(v) * (mean(v) - gm) ** 2 for v in groups.values())
    w = sum(sum((x - mean(v)) ** 2 for x in v) for v in groups.values())
    return (b / (b + w) * 100, w / (b + w) * 100) if (b + w) else (None, None)


# ---------- 读数据 ----------
def norm(s): return str(s).strip().lower().replace('_', '').replace('-', '')

def load(path, cols=None):
    with open(path, encoding='utf-8-sig', newline='') as f:
        rows = [r for r in csv.reader(f)]
    if not rows: sys.exit('空文件')

    idx = {}
    if cols:
        parts = cols.split(',')
        if len(parts) != 4: sys.exit('--cols 需要 4 个位置: worker,item,seg,aht（用 x 表示无此列）')
        for k, p in zip(['worker', 'item', 'seg', 'aht'], parts):
            idx[k] = None if p.strip().lower() == 'x' else int(p)
        probe = idx['seg'] if idx['seg'] is not None else idx['aht']
        start = 1 if not norm(rows[0][probe]).lstrip('-').replace('.', '', 1).isdigit() else 0
    else:
        head = [norm(c) for c in rows[0]]
        for k, names in ALIAS.items():
            for j, c in enumerate(head):
                if c in [norm(n) for n in names]:
                    idx[k] = j; break
            else:
                idx[k] = None
        if idx['aht'] is None:
            sys.exit('识别不到 AHT / 时长列，请用 --cols 指定，例如 --cols 0,1,2,3')
        start = 1

    out, dropped = [], []
    has_seg = idx['seg'] is not None
    for r in rows[start:]:
        try:
            if has_seg:
                sraw = r[idx['seg']].strip()
                if not sraw or not sraw.lstrip('-').replace('.', '', 1).isdigit(): continue
                seg = float(sraw)
            else:
                seg = None
            araw = r[idx['aht']].strip()
            w = r[idx['worker']].strip() if idx['worker'] is not None else ''
            it = r[idx['item']].strip() if idx['item'] is not None else ''
            if norm(araw) in BAD:
                dropped.append((w, it, seg, araw or '(空)')); continue
            out.append((w, it, seg, float(araw)))
        except (IndexError, ValueError):
            continue
    if not out: sys.exit('没有解析到有效行')
    return out, dropped


# ---------- 报告 ----------
def hr(t=''): print('\n' + '=' * 74 + ('\n' + t + '\n' + '=' * 74 if t else ''))

def main():
    ap = argparse.ArgumentParser(description='AHT 目标测算')
    ap.add_argument('data')
    ap.add_argument('--cols', help='按位置指定列: worker,item,size,aht（无此列填 x）')
    ap.add_argument('--size-label', default='segment',
                    help='规模变量在输出里的叫法，如 字数 / 图片数 / 工单复杂度（默认 segment）')
    ap.add_argument('--baseline', type=float, required=True, help='当前 AHT 基线（秒）')
    ap.add_argument('--baseline-is', choices=['short', 'mixed'], required=True,
                    help='short = 该值为「完全没有长 case」的基线；mixed = 该值为含长 case 的实测均值')
    ap.add_argument('--q0', type=float, help='baseline-is=mixed 时，基线对应的长 case 占比（如 0.10）')
    ap.add_argument('--q', type=float, action='append', help='要测算的占比，可多次传入（如 --q 0.10 --q 0.20）')
    ap.add_argument('--t-long', type=float, help='覆盖 T_长（有更新的实测值时用它，而不是本文件算出的）')
    ap.add_argument('--expect-seg', type=float, help='校验用：表格汇总行的均值规模值')
    ap.add_argument('--expect-aht', type=float, help='校验用：表格汇总行的均值 AHT')
    a = ap.parse_args()

    rows, dropped = load(a.data, a.cols)
    W = [r[0] for r in rows]; I = [r[1] for r in rows]
    S = [r[2] for r in rows]; T = [r[3] for r in rows]
    B = a.baseline
    SZ = a.size_label
    has_seg = S[0] is not None

    hr('① 数据概览')
    print(f'  有效 {len(rows)} 行  |  无效 {len(dropped)} 行 ({len(dropped)/(len(rows)+len(dropped))*100:.0f}%)')
    if any(I):
        nw = len(set(w for w in W if w))
        print(f'  不同 Item {len(set(I))}  |  不同处理人 {nw}'
              + ('   ⚠ 未识别到处理人列，配对分析将跳过' if nw == 0 else ''))
    if has_seg:
        print(f'  {SZ:<8} 均值 {mean(S):.2f}  中位 {median(S):.0f}  范围 {min(S):g}–{max(S):g}')
    print(f'  AHT      均值 {mean(T):.1f}s  中位 {median(T):.0f}s  SD {stdev(T):.0f}  SE {sem(T):.0f}')
    print(f'           P10 {pct(T,.1):.0f}  P25 {pct(T,.25):.0f}  P75 {pct(T,.75):.0f}  P90 {pct(T,.9):.0f}')
    print(f'           均值/中位 = {mean(T)/median(T):.2f}' + ('  ← 右偏，均值被尾部拖高' if mean(T)/median(T) > 1.15 else ''))
    if dropped:
        ds = [d[2] for d in dropped if d[2] is not None]
        if has_seg and ds:
            gap = (mean(ds) - mean(S)) / mean(S) if mean(S) else 0
            tag = ('  ← 缺失偏向长的一端，T_长 被低估' if gap > 0.05
                   else '  ← 缺失偏向短的一端' if gap < -0.05 else '  （无明显方向性）')
            print(f'  无效行的{SZ}均值 {mean(ds):.1f} vs 有值行 {mean(S):.1f}{tag}')
        kinds = sorted({d[3] for d in dropped})
        print(f'  无效类型: {", ".join(kinds)}')

    if a.expect_seg or a.expect_aht:
        hr('② 转录校验')
        for lab, got, exp in [(f'均值 {SZ}', mean(S) if has_seg else None, a.expect_seg),
                              ('均值 AHT', mean(T), a.expect_aht)]:
            if got is None: continue
            if exp is None: continue
            ok = abs(got - exp) < max(0.05, abs(exp) * 0.001)
            print(f'  {lab}: 复算 {got:.2f} / 表格 {exp:.2f}  {"✓ 一致" if ok else "✗ 对不上，先查转录"}')

    # ---- 配对 ----
    T_long = mean(T)
    if any(I) and any(W):
        by = defaultdict(list)
        for w, it, s, t in rows: by[it].append((w, s, t))
        pairs = {k: sorted(v, key=lambda x: x[2]) for k, v in by.items() if len(v) >= 2}
        hr('③ 配对分析（同一 Item 多人做过 = 天然对照实验）')
        if not pairs:
            print('  未发现重复 Item —— 无法分离「人」和「case 难度」')
        else:
            rat = [v[-1][2] / v[0][2] for v in pairs.values()]
            print(f'  {len(pairs)} 个 Item 被 ≥2 人做过，覆盖 {sum(len(v) for v in pairs.values())} 条记录\n')
            print(f'  慢/快比: 最小 {min(rat):.2f}  中位 {median(rat):.2f}  几何均值 {gmean(rat):.2f}  最大 {max(rat):.2f}')
            for th in (1.5, 2, 3):
                c = sum(1 for r in rat if r >= th)
                print(f'    ≥{th}x: {c}/{len(rat)} ({c/len(rat)*100:.0f}%)')
            print('\n  最极端的 5 组:')
            for k, v in sorted(pairs.items(), key=lambda p: -(p[1][-1][2] / p[1][0][2]))[:5]:
                sz = f'{v[0][1]:g} {SZ}' if v[0][1] is not None else ''
                print(f'    …{k[-6:]:>6}  {sz:>12}  {v[0][2]:>7.0f}s ({v[0][0]}) → '
                      f'{v[-1][2]:>7.0f}s ({v[-1][0]})  {v[-1][2]/v[0][2]:.2f}x')

            b, w = decompose({k: [math.log(x[2]) for x in v] for k, v in pairs.items()})
            print(f'\n  log(AHT) 方差分解（按 Item 分组，组内{SZ}恒定）:')
            print(f'    组间「哪个 case」  {b:.1f}%')
            print(f'    组内「谁在做」    {w:.1f}%   ← 与 case 规模完全无关的部分')

            rel = defaultdict(list)
            for v in pairs.values():
                g = gmean([x[2] for x in v])
                for wk, s, t in v: rel[wk].append(t / g)
            hr('④ 各处理人相对同 case 平均水平（已消去 case 难度）')
            ks = sorted(((gmean(v), k, len(v)) for k, v in rel.items()))
            for g, k, n in ks:
                bar = '█' * max(1, int(min(g, 3) * 12))
                print(f'    {k:>16} n={n:>2}  {g:>5.2f}x  {bar}')
            solid = [g for g, k, n in ks if n >= 3]
            if len(solid) >= 2:
                print(f'\n    n≥3 的人之间跨度 {max(solid)/min(solid):.1f}x（{min(solid):.2f}x ↔ {max(solid):.2f}x）')
            print('\n  ⚠ 先确认这是「两人独立做同一件事」还是「一做一审」——后者两个时长不可比')

    # ---- 规模变量关系 ----
    if not has_seg:
        hr('⑤ 规模变量')
        print(f'  数据里没有规模列，跳过。混合均值模型不依赖它，但少了「时间花在哪」的拆解')
    else:
      hr(f'⑤ {SZ} 的作用')
      a0, b0, r2, se_b = ols(S, [math.log(t) for t in T])
      if True:
        print(f'  log(AHT) ~ {SZ}:  斜率 {b0:+.4f} ± {se_b:.4f}   t = {b0/se_b:.2f}   R² = {r2:.3f}')
        print(f'  每 10 个单位  ×{math.exp(b0*10):.3f}')
        sig = abs(b0 / se_b) >= 2
        print(f'  → {"统计显著" if sig else "不显著"}；解释力 {r2*100:.1f}%')
        if r2 < 0.15:
            print('    解释力低 ≠ 无关：先确认有没有更强的混杂因素（人、难度、挂机）把信号淹了')
      g = defaultdict(list)
      for _, _, s, t in rows: g[s].append(math.log(t))
      bb, ww = decompose(g)
      if bb is not None:
        print(f'  按 {SZ} 取值分组: 组间 {bb:.1f}%  组内 {ww:.1f}%')

    # ---- 离群点 ----
    hr('⑥ 数据质量')
    srt = sorted(T, reverse=True)
    print(f'  逐个剔除最大值后的均值变化:')
    for k in range(0, min(4, len(T))):
        sub = srt[k:]
        print(f'    剔除 {k} 个: n={len(sub)}  均值 {mean(sub):.1f}s'
              + (f'   (较全样本 {mean(sub)-mean(T):+.0f}s)' if k else ''))
    if len(T) > 1 and mean(T) - mean(srt[1:]) > mean(T) * 0.05:
        print(f'  ⚠ 单条记录 {srt[0]:.0f}s 就把均值抬高了 {mean(T)-mean(srt[1:]):.0f}s —— 查是否为挂机')

    # ---- 目标曲线 ----
    if a.t_long:
        T_long = a.t_long
        src = f'外部指定 {T_long:.0f}s'
    else:
        T_long = mean(T)
        src = f'本文件 {len(T)} 条实测均值'
    se_L = sem(T) if not a.t_long else 0.0

    if a.baseline_is == 'short':
        T_short = B
        note = f'基线 {B:.0f}s = 无长 case 时的 T_短'
    else:
        if a.q0 is None: sys.exit('--baseline-is mixed 时必须给 --q0')
        T_short = (B - a.q0 * T_long) / (1 - a.q0)
        note = f'基线 {B:.0f}s 为 q₀={a.q0*100:.2f}% 的混合均值 → 反解 T_短 = {T_short:.1f}s'

    slope = (T_long - T_short) / 100
    hr('⑦ 目标测算')
    print(f'  T_长  = {T_long:.1f}s   ({src})')
    print(f'  T_短  = {T_short:.1f}s   ({note})')
    print(f'  斜率  = {slope:+.2f} s / 每 1 个百分点\n')
    print(f'  目标 = {T_short:.0f} + {slope:.2f} × 占比(%)\n')

    qs = a.q or [0.05, 0.075, 0.10, 0.15, 0.20]
    print(f'  {"占比":>8} {"增量":>8} {"目标":>8} {"95% 区间":>16}')
    for q in sorted(set(qs)):
        tgt = T_short + q * (T_long - T_short)
        lo = T_short + q * (T_long - 1.96 * se_L - T_short)
        hi = T_short + q * (T_long + 1.96 * se_L - T_short)
        ci = f'[{lo:.0f}, {hi:.0f}]' if se_L else '（T_长为外部值）'
        print(f'  {q*100:>7.2f}% {q*(T_long-T_short):>+8.0f} {tgt:>8.0f} {ci:>16}')

    obs_max = None
    if a.q:
        far = [q for q in a.q if q > 0.12]
        if far:
            print(f'\n  ⚠ {", ".join(f"{q*100:.0f}%" for q in far)} 属于外推。代入前先确认:')
            print(f'     · 长 case 的平均{SZ}往哪走？降→T_长 调低，升→T_长 调高')
            print('     · 有实测的新 T_长 就用实测值，别拿旧 T_长 线性外推（用 --t-long 覆盖）')

    print(f'\n  建议给动态形式而非单个数字:  当日目标 = {T_short:.0f} + {slope:.2f} × 当天实际占比(%)')
    if any(I) and any(W):
        try:
            span = max(solid) / min(solid)
            print(f'\n  量级对比: 占比每变 1pp 只值 {abs(slope):.1f}s，'
                  f'而人效跨度 {span:.1f}x —— 该目标适合产能规划，不适合直接当个人考核线')
        except (NameError, ValueError, ZeroDivisionError):
            pass
    print()


if __name__ == '__main__':
    main()
