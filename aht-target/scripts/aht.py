#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AHT 目标测算：长 case 占比变了，AHT（平均每条处理时长）目标该给多少。

输入：一个 CSV，只放长 case 的逐条明细（至少要有处理时长这一列）。
有标注员和 case ID 列时，还会分析「同一个 case 换个人做差多少」。

用法:
  python3 aht.py 长case明细.csv --baseline 500 --baseline-is short --q 20%
  python3 aht.py 长case明细.csv --baseline 500 --baseline-is mixed --q0 10% --q 15%
  python3 aht.py 长case明细.csv --baseline 500 --baseline-is short --q 20% --t-long 1800

  --baseline-is short：基线是「完全没有长 case 时」的水平
  --baseline-is mixed：基线是「现在所有 case 的平均」，这时要用 --q0 说明当时长 case 占多少
  占比写 20% 或 0.2 都行

列名自动识别（不区分大小写、忽略空格）；识别不到时用 --cols 按位置指定：
  worker / annotator / 标注员      标注员 ID  （可选，有则做换人对比）
  item / item id / case / 任务      case ID    （可选，有则做换人对比）
  segment / size / 字数…            规模（可选，缺了只是少一段分析）
  aht / duration / 时长             处理时长（秒）

非数值的 AHT（deferred / In check / Not found / 空）自动剔除并单独统计。
"""
import argparse, csv, math, sys
from collections import defaultdict

BAD = {'deferred', 'defered', 'in check', 'incheck', 'not found', 'notfound', 'n/a', 'na', '-', ''}
ALIAS = {
    'worker': ['worker', 'annotator', 'user', 'operator', 'agent', 'assignee', 'owner', 'reviewer',
               '标注员', '员工', '账号', '处理人', '操作人', '处理员', '客服', '审核员', '质检员', '作业员'],
    'item':   ['item', 'itemid', 'item id', 'case', 'caseid', 'case id', 'task', 'taskid', 'ticket',
               'ticketid', 'jobid', 'id', '任务', '任务id', '工单', '工单号', '单号', '案件', 'caseno'],
    # 规模变量：换项目时这里加你自己的列名
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


# ---------- 小工具 ----------
def pct_arg(s):
    """占比：20% / 20 / 0.2 都当作 20%"""
    t = str(s).strip()
    try:
        v = float(t.rstrip('%'))
    except ValueError:
        raise argparse.ArgumentTypeError(f'占比写成 20% 或 0.2 这样：{s}')
    if t.endswith('%') or v > 1:
        v /= 100
    if not 0 <= v < 1:
        raise argparse.ArgumentTypeError(f'占比要在 0%–100% 之间：{s}')
    return v

def dur_word(x):
    return f'{x / 3600:.1f} 小时' if x >= 3600 else f'{x / 60:.0f} 分钟' if x >= 60 else f'{x:.0f} 秒'

CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩'
_sec = [0]
def hr(t):
    _sec[0] += 1
    print('\n' + '=' * 74 + f'\n{CIRCLED[_sec[0] - 1]} {t}\n' + '=' * 74)


# ---------- 读数据 ----------
def norm(s): return str(s).strip().lower().replace('_', '').replace('-', '')

def load(path, cols=None):
    with open(path, encoding='utf-8-sig', newline='') as f:
        rows = [r for r in csv.reader(f)]
    if not rows: sys.exit('空文件')

    idx = {}
    if cols:
        parts = cols.split(',')
        if len(parts) != 4: sys.exit('--cols 需要 4 个位置：标注员,caseID,规模,AHT（没有的填 x），如 0,1,2,3')
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
            sys.exit('认不出处理时长（AHT）这一列，请用 --cols 按位置指定，如 --cols 0,1,2,3')
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
            try:                                   # 不是数字的一律算无效，不管平台叫它什么
                if norm(araw) in BAD: raise ValueError
                v = float(araw.replace(',', ''))
            except ValueError:
                dropped.append((w, it, seg, araw or '(空)')); continue
            if v <= 0:
                dropped.append((w, it, seg, '0 或负数')); continue
            out.append((w, it, seg, v))
        except (IndexError, ValueError):
            continue
    if not out: sys.exit('没有读到有效的数据行')
    return out, dropped


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser(
        description='AHT 目标测算：长 case 占比变了，AHT（平均每条处理时长）目标该给多少',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='例子：\n'
               '  python3 aht.py 长case明细.csv --baseline 500 --baseline-is short --q 20%\n'
               '  （基线 500 秒是完全没有长 case 时的水平，算长 case 占 20% 时的目标）')
    ap.add_argument('data', help='CSV 文件，只放长 case 的逐条明细')
    ap.add_argument('--cols', help='按位置指定列：标注员,caseID,规模,AHT（从 0 数起，没有的填 x），如 0,1,2,3')
    ap.add_argument('--size-label', default='segment',
                    help='规模在输出里的叫法，如 字数、图片数（默认 segment）')
    ap.add_argument('--baseline', type=float, required=True, help='现在的 AHT 基线（秒）')
    ap.add_argument('--baseline-is', choices=['short', 'mixed'], required=True,
                    help='short = 基线是完全没有长 case 时的水平；mixed = 基线是现在所有 case 的平均')
    ap.add_argument('--q0', type=pct_arg, help='baseline-is 为 mixed 时，基线对应的长 case 占比，如 10%%')
    ap.add_argument('--q', type=pct_arg, action='append', help='想算的长 case 占比，可写多个，如 --q 15%% --q 20%%')
    ap.add_argument('--q-now', type=pct_arg, help='现在实际的长 case 占比，如 7%%。给了之后能判断要算的占比是不是往外推')
    ap.add_argument('--t-long', type=float, help='长 case 的平均时间（秒）。有更新的实测值时，用它代替本文件算出的')
    ap.add_argument('--expect-seg', type=float, help='核对用：表格汇总行里的平均规模')
    ap.add_argument('--expect-aht', type=float, help='核对用：表格汇总行里的平均 AHT')
    ap.add_argument('--tech', action='store_true', help='多显示统计细节（R²、t 值、几何平均等），给懂统计的人看')
    a = ap.parse_args()

    rows, dropped = load(a.data, a.cols)
    W = [r[0] for r in rows]; I = [r[1] for r in rows]
    S = [r[2] for r in rows]; T = [r[3] for r in rows]
    B, SZ = a.baseline, a.size_label
    sm = f' {SZ} ' if SZ.isascii() else SZ    # 句中：英文词两侧留空格
    sh = f'{SZ} ' if SZ.isascii() else SZ     # 句首
    has_seg = S[0] is not None
    kinds = '、'.join(sorted({d[3] for d in dropped}))

    # ---- 先把所有东西算完 ----
    miss = None
    ds = [d[2] for d in dropped if d[2] is not None]
    if has_seg and ds and mean(S):
        miss = (mean(ds), mean(S), (mean(ds) - mean(S)) / mean(S))

    T_long = a.t_long if a.t_long else mean(T)
    se_L = 0.0 if a.t_long else sem(T)
    if a.baseline_is == 'short':
        T_short = B
    else:
        if a.q0 is None:
            sys.exit('--baseline-is mixed 时，要用 --q0 说明基线对应的长 case 占比，如 --q0 10%')
        T_short = (B - a.q0 * T_long) / (1 - a.q0)
    slope = (T_long - T_short) / 100
    asked = sorted(set(a.q)) if a.q else []
    qs = asked or [0.05, 0.075, 0.10, 0.15, 0.20]
    def target(q, TL=None): return T_short + q * ((T_long if TL is None else TL) - T_short)
    def rng(q): return target(q, T_long - 1.96 * se_L), target(q, T_long + 1.96 * se_L)
    far = [q for q in asked if (q >= a.q_now * 1.5 if a.q_now else q > 0.12)]
    far_txt = '、'.join(f'{q * 100:g}%' for q in far)
    far_head = (f'{far_txt} 比现在的 {a.q_now * 100:g}% 高出不少' if a.q_now
                else f'如果现在的实际占比还不到 {max(far) * 50:g}%（{max(far) * 100:g}% 的一半）' if far else '')

    srt = sorted(T, reverse=True)
    top_effect = mean(T) - mean(srt[1:]) if len(T) > 1 else 0.0
    big_outlier = len(T) > 1 and top_effect > mean(T) * 0.05

    pairs, rat, split, ks, span = {}, [], None, [], None
    paired_data = any(I) and any(W)
    if paired_data:
        by = defaultdict(list)
        for w, it, s, t in rows: by[it].append((w, s, t))
        pairs = {k: sorted(v, key=lambda x: x[2]) for k, v in by.items() if len(v) >= 2}
        if pairs:
            rat = [v[-1][2] / v[0][2] for v in pairs.values()]
            split = decompose({k: [math.log(x[2]) for x in v] for k, v in pairs.items()})
            rel = defaultdict(list)
            for v in pairs.values():
                g = gmean([x[2] for x in v])
                for wk, s, t in v: rel[wk].append(t / g)
            ks = sorted((gmean(v), k, len(v)) for k, v in rel.items())
            solid = [g for g, k, n in ks if n >= 3]
            if len(solid) >= 2: span = (min(solid), max(solid))

    # ---- 结论 ----
    print('\n【结论】')
    print(f'  长 case 平均每条 {T_long:.0f} 秒'
          + ('（你用 --t-long 指定的）' if a.t_long else f'（按本文件 {len(T)} 条有效记录算）'))
    if a.baseline_is == 'short':
        print(f'  短 case 每条 {T_short:.0f} 秒（你给的基线：完全没有长 case 时的水平）')
    else:
        print(f'  短 case 每条 {T_short:.0f} 秒（你给的 {B:.0f} 秒是长 case 占 {a.q0 * 100:g}% 时的整体平均，由此倒推）')
    print(f'  长 case 占比每涨 1 个百分点，AHT 目标{"加" if slope >= 0 else "减"} {abs(slope):.1f} 秒')
    print(f'      目标 = {T_short:.0f} + {slope:.1f} × 长 case 占比(%)')
    for q in asked:
        lo, hi = rng(q)
        print(f'  长 case 占 {q * 100:g}% → 目标 {target(q):.0f} 秒'
              + (f'（大概率在 {lo:.0f}–{hi:.0f} 秒之间）' if se_L else ''))
    if not asked:
        print('  各占比对应的目标见最后一节的表')

    notes = []
    if big_outlier:
        notes.append(f'⚠ 有一条记录 {srt[0]:.0f} 秒（约 {dur_word(srt[0])}），单这一条就把长 case 的平均拉高了 '
                     f'{top_effect:.0f} 秒。像是挂机，建议核实')
    if miss and miss[2] > 0.05:
        notes.append(f'⚠ 有 {len(dropped)} 条没有有效时间（{kinds}），它们的{sm}平均 {miss[0]:.1f}，比有效记录的 '
                     f'{miss[1]:.1f} 高：最费时的 case 恰好缺数据，长 case 的平均时间可能算低了')
    if far:
        notes.append(f'⚠ {far_head}，这个结果就是按现有数据往外推的。长 case 变多时，长 case 本身的平均时间也可能跟着变（见最后一节）'
                     + ('' if a.q_now else '。用 --q-now 说明现在的占比，可以判断得更准'))
    if rat:
        extra = f'；做过 3 条以上的人里，最快和最慢的差 {span[1] / span[0]:.1f} 倍' if span else ''
        notes.append(f'· 同一个 case 换个人做，一般慢的人是快的人的 {gmean(rat):.1f} 倍{extra}。'
                     f'这个目标适合排产、估人力，不适合直接拿来考核个人')
    notes.append('· 如果是要定长 case 的目标，这个文件应该只放长 case，混进短 case 会把长 case 的平均算低。'
                 '只是看换人差多少的话，放全部 case 没关系')
    print('\n【需要注意】')
    for x in notes: print('  ' + x)
    print('\n下面是详细数据 ↓')

    # ---- 数据概览 ----
    hr('数据概览')
    print(f'  有效 {len(rows)} 条，无效 {len(dropped)} 条（{len(dropped) / (len(rows) + len(dropped)) * 100:.0f}%）'
          + (f'：{kinds}' if dropped else ''))
    if any(I):
        nw = len(set(w for w in W if w))
        print(f'  {len(set(I))} 个不同的 case，{nw} 个处理人'
              + ('   ⚠ 没认出处理人这一列，换人对比会跳过' if nw == 0 else ''))
    if has_seg:
        print(f'  {SZ}：平均 {mean(S):.1f}，最少 {min(S):g}，最多 {max(S):g}')
    else:
        print('  没有规模这一列：少了「时间花在哪」的分析，目标照样能算')
    print(f'  AHT：平均 {mean(T):.0f} 秒，中间值 {median(T):.0f} 秒')
    print(f'       一半的记录在 {pct(T, .25):.0f}–{pct(T, .75):.0f} 秒之间；'
          f'最快的 10% 不到 {pct(T, .1):.0f} 秒，最慢的 10% 超过 {pct(T, .9):.0f} 秒')
    r_ = mean(T) / median(T)
    if r_ > 1.15:
        print(f'       平均值比中间值高 {(r_ - 1) * 100:.0f}%：有少数特别慢的记录把平均拉高了')
    if miss:
        tag = ('→ 缺数据的偏向长的一端，长 case 的平均时间可能算低了' if miss[2] > 0.05
               else '→ 缺数据的偏向短的一端' if miss[2] < -0.05 else '→ 看不出偏向')
        print(f'  无效记录的{sm}平均 {miss[0]:.1f}，有效记录 {miss[1]:.1f}  {tag}')

    # ---- 核对表格汇总 ----
    if a.expect_seg or a.expect_aht:
        hr('核对表格汇总')
        for lab, got, exp in [(f'平均{SZ}', mean(S) if has_seg else None, a.expect_seg),
                              ('平均 AHT', mean(T), a.expect_aht)]:
            if got is None or exp is None: continue
            ok = abs(got - exp) < max(0.05, abs(exp) * 0.001)
            print(f'  {lab}：重新算出 {got:.2f}，表格写的 {exp:.2f}  '
                  + ('✓ 一致' if ok else '✗ 对不上，先检查数据有没有抄错'))

    # ---- 换人对比 ----
    if paired_data:
        hr('同一个 case 换个人做，差多少')
        if not pairs:
            print('  没有 case 被两个人以上做过，没法把「人的差异」和「case 的差异」分开')
        else:
            print(f'  有 {len(pairs)} 个 case 被两个人以上做过（共 {sum(len(v) for v in pairs.values())} 条记录）')
            print(f'  慢的人一般是快的人的 {gmean(rat):.1f} 倍（最少 {min(rat):.2f} 倍，最多 {max(rat):.2f} 倍）')
            for th in (1.5, 2, 3):
                c = sum(1 for r in rat if r >= th)
                print(f'    差 {th:g} 倍以上：{c}/{len(rat)}（{c / len(rat) * 100:.0f}%）')
            print('\n  差得最多的 5 组：')
            for k, v in sorted(pairs.items(), key=lambda p: -(p[1][-1][2] / p[1][0][2]))[:5]:
                sz = f'{v[0][1]:g} {SZ}' if v[0][1] is not None else ''
                print(f'    case …{k[-6:]:>6}  {sz:>12}  {v[0][2]:>7.0f} 秒（{v[0][0]}）→ '
                      f'{v[-1][2]:>7.0f} 秒（{v[-1][0]}）  差 {v[-1][2] / v[0][2]:.2f} 倍')
            b, w = split
            print(f'\n  时间差异从哪来（同一个 case 的{sm}是一样的，所以换人造成的差异跟长短无关）：')
            print(f'    case 本身不同   {b:.1f}%   （长短、难易、内容上的差别都算在里面）')
            print(f'    换了个人       {w:.1f}%')
            if a.tech:
                print(f'  （技术细节：倍数取几何平均，中位数 {median(rat):.2f}；按 case 分组，对 log(AHT) 做方差分解）')
            print('\n  ⚠ 先确认：这是两个人各自独立做同一个 case，还是一个人做、一个人审？'
                  '后者两个时间做的不是同一件事，不能这么比')
        if ks:
            hr('每个人的快慢（和同一个 case 的平均比）')
            print('  1.00x = 平均水平，小于 1 比平均快，大于 1 比平均慢。只用被多人做过的 case，已排除 case 难易的影响\n')
            for g, k, n in ks:
                bar = '█' * max(1, int(min(g, 3) * 12))
                print(f'    {k:>16}  做过 {n:>2} 条  {g:>5.2f}x  {bar}')
            if span:
                print(f'\n  做过 3 条以上的人之间：最快 {span[0]:.2f}x，最慢 {span[1]:.2f}x，差 {span[1] / span[0]:.1f} 倍')
            print('  只做过 1–2 条的人，结果不稳定，仅供参考')

    # ---- 规模的影响 ----
    if has_seg:
        hr(f'{sh}对时间的影响')
        a0, b0, r2, se_b = ols(S, [math.log(t) for t in T])
        if b0 is None:
            print('  数据太少，或者规模都一样，算不了')
        else:
            k = 10 ** max(0, round(math.log10(max(median(S), 1))) - 1)
            ch = math.exp(b0 * k) - 1
            sig = se_b and abs(b0 / se_b) >= 2
            print(f'  {sh}每多 {k:g}，时间大约{"多" if ch >= 0 else "少"} {abs(ch) * 100:.0f}%')
            print(f'  {sh}能解释时间差异的 {r2 * 100:.0f}%：'
                  + (('这个影响是真实存在的' + ('，只是不大' if r2 < 0.15 else '')) if sig else '看不出确定的影响，可能只是巧合'))
            if split and split[0] is not None and r2 * 100 < split[0]:
                print(f'  和前面对照：「case 本身不同」占 {split[0]:.0f}%，其中长短只占一小部分，'
                      f'其余来自难易、内容这些长短以外的差别')
            if r2 < 0.15:
                print('  解释得少 ≠ 没关系：可能是人和人的差异、挂机记录太大，把长短的影响盖住了')
            if a.tech:
                print(f'  （技术细节：log(AHT) 对{sm}回归，斜率 {b0:+.4f} ± {se_b:.4f}，t = {b0 / se_b:.2f}，R² = {r2:.3f}）')

    # ---- 特别慢的记录 ----
    hr('特别慢的记录')
    print('  去掉最慢的几条后，平均值怎么变：')
    for k in range(min(4, len(T))):
        sub = srt[k:]
        head = '全部' if k == 0 else f'去掉最慢 {k} 条'
        print(f'    {head}：{len(sub)} 条，平均 {mean(sub):.0f} 秒'
              + (f'（少了 {mean(T) - mean(sub):.0f} 秒）' if k else ''))
    if big_outlier:
        print(f'  ⚠ 单独一条 {srt[0]:.0f} 秒（约 {dur_word(srt[0])}）就把平均拉高了 {top_effect:.0f} 秒，像是挂机，建议核实')
    else:
        print('  没有哪一条能单独把平均拉偏太多')

    # ---- 目标怎么算 ----
    hr('目标怎么算')
    print(f'  长 case 平均每条 {T_long:.0f} 秒'
          + ('（你用 --t-long 指定的）' if a.t_long else f'（本文件 {len(T)} 条有效记录的平均）'))
    if a.baseline_is == 'short':
        print(f'  短 case 每条 {T_short:.0f} 秒（你给的基线，就是完全没有长 case 时的水平）')
    else:
        print(f'  短 case 每条 {T_short:.0f} 秒（你给的 {B:.0f} 秒是长 case 占 {a.q0 * 100:g}% 时的整体平均，由此倒推）')
    print(f'  长 case 占比每涨 1 个百分点，目标{"加" if slope >= 0 else "减"} '
          f'({T_long:.0f} − {T_short:.0f}) ÷ 100 = {abs(slope):.1f} 秒')
    print(f'  目标 = {T_short:.0f} + {slope:.1f} × 长 case 占比(%)\n')
    print('  长 case 占比    比基线多      目标      大概率在')
    for q in qs:
        lo, hi = rng(q)
        print(f'  {q * 100:>9g}%   {q * (T_long - T_short):>+7.0f} 秒  {target(q):>6.0f} 秒   '
              + (f'{lo:.0f}–{hi:.0f} 秒' if se_L else '（长 case 平均时间是指定值，没有范围）'))
    if far:
        print(f'\n  ⚠ {far_head}，代入前先确认：')
        print(f'     · 长 case 的平均{sm}往哪走：变少了，长 case 平均时间要往下调；变多了，要往上调')
        print('     · 手上有更新的长 case 平均时间，就用 --t-long 代进来，不要拿旧的数硬推')
    print(f'\n  建议：每天按当天实际的长 case 占比算目标 → 当日目标 = {T_short:.0f} + {slope:.1f} × 当天长 case 占比(%)')
    print()


if __name__ == '__main__':
    main()
