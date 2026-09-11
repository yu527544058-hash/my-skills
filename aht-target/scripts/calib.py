#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按视频时长（或任何连续的规模变量）定 AHT 目标：试标定标 → 生产重标 → 每批出目标。

    每条处理时间 = 起步时间 + 每秒多花 × 视频时长

  起步时间：不管视频多长都要花的时间（打开、加载、看要求、检查、提交）
  每秒多花：视频每长 1 秒，要多花的处理时间

用法:
  # 阶段 1：项目经理试标定标（前 4 条是热身，不计入；生产视频平均 75 秒）
  python3 calib.py fit trial.csv --warmup 4 --prod-mean 75 --save pm.json

  # 阶段 2：上线 2–3 天后用生产数据重标，顺带算出「项目经理 → 标注员」换算系数
  python3 calib.py fit prod.csv --vs pm.json --save prod.json

  # 阶段 3：每批出目标（给这批平均时长，或给时长列表）
  python3 calib.py predict --model prod.json --mean 82
  python3 calib.py predict --model prod.json --durations batch.csv --per-case out.csv

列名自动识别（视频时长 / duration / AHT / 处理时长 / case id / 顺序 / 标注员）。
时长和 AHT 都支持：秒数、mm:ss、h:mm:ss。
非数值的 AHT（deferred / In check / Not found / 空）自动剔除并单独列出。
"""
import argparse, csv, datetime, json, math, sys

BAD = {'deferred', 'defered', 'incheck', 'notfound', 'n/a', 'na', '-', ''}
ALIAS = {   # 按优先级排列，越靠前越优先
    'aht':    ['aht', '处理时长', '标注时长', '耗时', '工时', 'handlingtime', 'handletime', 'time'],
    'dur':    ['视频时长', 'videoduration', 'videolength', '视频长度', 'duration', 'video', 'length',
               'dur', '时长', 'segment', 'segmentnumber', 'size'],
    'id':     ['caseid', 'itemid', 'case', 'item', 'id', '任务id', '编号', 'taskid'],
    'order':  ['标注顺序', '顺序', '序号', 'order', 'seq', 'sequence', 'no', '#'],
    'worker': ['标注员', '处理人', 'annotator', 'worker', 'operator', 'user', '账号'],
}


# ---------------- 小工具 ----------------
def norm(s):
    return str(s).strip().lower().replace('_', '').replace('-', '').replace(' ', '')

def to_sec(s):
    s = str(s).strip().lower().replace('秒', '').rstrip('s').strip()
    if not s:
        raise ValueError
    if ':' in s:
        return sum(float(p) * 60 ** i for i, p in enumerate(reversed(s.split(':'))))
    return float(s)

def fmt(sec):
    sign = '-' if sec < 0 else ''
    m, s = divmod(int(round(abs(sec))), 60)
    return f'{sign}{m}分{s:02d}秒' if m else f'{sign}{s}秒'

def ceil10(x): return int(math.ceil(x / 10.0) * 10)
def mean(v): return sum(v) / len(v)
def median(v):
    s = sorted(v); n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
def stdev(v):
    m = mean(v); return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1)) if len(v) > 1 else 0.0
def gmean(v): return math.exp(mean([math.log(x) for x in v]))

def t975(df):
    """t 分布 97.5% 分位数（Cornish-Fisher 近似，df≥3 时误差 <0.5%）"""
    if df <= 0: return float('nan')
    z = 1.959964
    return (z + (z**3 + z) / (4 * df) + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * df**2)
            + (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / (384 * df**3))

def hr(t): print('\n' + '=' * 68 + '\n' + t + '\n' + '=' * 68)

UNIT = '秒'   # 规模变量的单位；视频时长是秒，其他如 segment / 字
def fd(d): return fmt(d) if UNIT == '秒' else f'{round(d, 1):g} {UNIT}'
def rate(): return '每秒多花' if UNIT == '秒' else f'每{UNIT}多花'
def lbl(): return '视频时长' if UNIT == '秒' else '规模'


# ---------------- 读表 ----------------
def pick(head, key, exclude=()):
    for name in [norm(n) for n in ALIAS[key]]:
        for j, c in enumerate(head):
            if j not in exclude and c == name:
                return j
    return None

def resolve(head, spec):
    if spec.isdigit(): return int(spec)
    for j, c in enumerate(head):
        if c == norm(spec): return j
    sys.exit(f'找不到列「{spec}」，现有列：{", ".join(head)}')

def load(path, dur_col=None, aht_col=None, need_aht=True):
    with open(path, encoding='utf-8-sig', newline='') as f:
        rows = [r for r in csv.reader(f) if any(c.strip() for c in r)]
    if len(rows) < 2: sys.exit(f'{path} 没有数据行')
    head = [norm(c) for c in rows[0]]
    ia = (resolve(head, aht_col) if aht_col else pick(head, 'aht')) if need_aht else None
    idur = resolve(head, dur_col) if dur_col else pick(head, 'dur', exclude={ia})
    if idur is None or (need_aht and ia is None):
        sys.exit('需要「视频时长」和「AHT」两列，识别不到时用 --dur-col / --aht-col 指定列名或位置')
    used = {ia, idur}
    iid, iord, iw = (pick(head, k, exclude=used) for k in ('id', 'order', 'worker'))

    ok, dropped = [], []
    for k, r in enumerate(rows[1:]):
        r = r + [''] * len(head)
        try:
            d = to_sec(r[idur])
        except ValueError:
            continue
        rec = {'id': r[iid].strip() if iid is not None else f'第{k + 1}行', 'dur': d, 'pos': k,
               'order': r[iord].strip() if iord is not None else None,
               'worker': r[iw].strip() if iw is not None else ''}
        if need_aht:
            raw = r[ia].strip()
            try:
                if norm(raw) in BAD: raise ValueError
                rec['aht'] = to_sec(raw)
            except ValueError:
                rec['raw'] = raw or '(空)'; dropped.append(rec); continue
        ok.append(rec)

    if iord is not None:
        def key(x):
            try: return (0, float(x['order']))
            except (TypeError, ValueError): return (1, x['order'] or '')
        ok.sort(key=key)
    return ok, dropped, iord is not None, iw is not None


# ---------------- 拟合 ----------------
def fit_linear(x, y):
    n = len(x); xb, yb = mean(x), mean(y)
    sxx = sum((v - xb) ** 2 for v in x)
    b = sum((xi - xb) * (yi - yb) for xi, yi in zip(x, y)) / sxx
    a = yb - b * xb
    res = [yi - (a + b * xi) for xi, yi in zip(x, y)]
    sst = sum((yi - yb) ** 2 for yi in y)
    s = math.sqrt(sum(r * r for r in res) / (n - 2))
    return {'mode': 'linear', 'a': a, 'b': b, 'n': n, 's': s, 'xbar': xb, 'ybar': yb, 'sxx': sxx,
            'sxx0': sum(v * v for v in x), 'r2': 1 - sum(r * r for r in res) / sst if sst else 0.0,
            'res': res}

def fit_origin(x, y):
    n = len(x); sxx0 = sum(v * v for v in x)
    b = sum(xi * yi for xi, yi in zip(x, y)) / sxx0
    res = [yi - b * xi for xi, yi in zip(x, y)]
    return {'mode': 'origin', 'a': 0.0, 'b': b, 'n': n, 's': math.sqrt(sum(r * r for r in res) / (n - 1)),
            'xbar': mean(x), 'ybar': mean(y), 'sxx': sum((v - mean(x)) ** 2 for v in x), 'sxx0': sxx0,
            'res': res}

def fit_flat(x, y):
    return {'mode': 'flat', 'a': mean(y), 'b': 0.0, 'n': len(y), 's': stdev(y), 'xbar': mean(x),
            'ybar': mean(y), 'sxx': sum((v - mean(x)) ** 2 for v in x), 'sxx0': sum(v * v for v in x),
            'res': [yi - mean(y) for yi in y]}

def pred(M, d):
    return M['a'] + M['b'] * d

def pred_ci(M, dbar):
    """某批次平均时长 dbar 下的目标值及 95% 区间"""
    n, s = M['n'], M['s']
    if M['mode'] == 'flat':
        se, df = s / math.sqrt(n), n - 1
    elif M['mode'] == 'origin':
        se, df = s * dbar / math.sqrt(M['sxx0']), n - 1
    else:
        se, df = s * math.sqrt(1 / n + (dbar - M['xbar']) ** 2 / M['sxx']), n - 2
    y = pred(M, dbar); t = t975(df)
    return y, y - t * se, y + t * se

def explain(M):
    if M['mode'] == 'flat':
        print(f'  不按{lbl()}调：每条都按 {fmt(M["a"])}（{M["a"]:.0f}s）算')
        return
    if M['mode'] == 'origin':
        print(f'  起步时间    按 0 处理（完全按比例）')
    else:
        print(f'  起步时间    {fmt(M["a"])}（{M["a"]:.0f}s）   ← 不管视频多长都要花的')
    if UNIT == '秒':
        print(f'  每秒多花    {M["b"]:.2f} 秒              ← 视频每长 1 秒，多花这么多')
        print(f'              即视频每长 1 分钟，多花 {fmt(M["b"] * 60)}')
    else:
        print(f'  每{UNIT}多花  {M["b"]:.2f} 秒              ← 每多 1 {UNIT}，多花这么多')


# ---------------- fit ----------------
def cmd_fit(a):
    global UNIT
    UNIT = a.unit
    is_trial = not a.vs and '项目经理' in a.level
    rows, dropped, has_order, has_worker = load(a.data, a.dur_col, a.aht_col)
    hr('① 数据')
    print(f'  有效 {len(rows)} 条' + (f'，剔除 {len(dropped)} 条无效 AHT：'
          + '、'.join(f'{d["id"]}({d["raw"]})' for d in dropped[:6]) if dropped else ''))
    print(f'  顺序依据：{"表里的顺序列" if has_order else "文件里的行顺序（没找到顺序列）"}')

    # 热身期
    if a.warmup:
        print(f'  按要求去掉前 {a.warmup} 条热身：' + '、'.join(r['id'] for r in rows[:a.warmup]))
        rows = rows[a.warmup:]
    elif len(rows) >= 15 and is_trial:
        head5, rest = rows[:5], rows[5:]
        Mr = fit_linear([r['dur'] for r in rest], [r['aht'] for r in rest])
        over = mean([r['aht'] / max(pred(Mr, r['dur']), 1) for r in head5]) - 1
        if over > 0.15:
            print(f'  ⚠ 前 5 条平均比后面的规律多花 {over * 100:.0f}% —— 像是热身期。'
                  f'建议加 --warmup 5 重跑')
        else:
            print(f'  前 5 条和后面的规律差别不大（{over * 100:+.0f}%），没有明显热身期')

    if len(rows) < 8:
        sys.exit(f'只剩 {len(rows)} 条，太少了，至少要 8 条才能定标')

    x = [r['dur'] for r in rows]; y = [r['aht'] for r in rows]
    dmin, dmax = min(x), max(x)
    print(f'  {lbl():<8}{fd(dmin)} – {fd(dmax)}，平均 {fd(mean(x))}')
    print(f'  AHT       {fmt(min(y))} – {fmt(max(y))}，平均 {fmt(mean(y))}')

    hr('② 数据质量')
    warn = 0
    narrow = dmax / max(dmin, 1e-9) < 2
    if narrow:
        print(f'  ⚠ 最长的只是最短的 {dmax / dmin:.1f} 倍 —— 跨度太窄，算不准「{rate()}」。'
              f'试标要短、中、长都挑'); warn += 1
    L = fit_linear(x, y)
    big = [(r, e) for r, e in zip(rows, L['res']) if abs(e) > 2.5 * L['s']]
    for r, e in big:
        print(f'  ⚠ {r["id"]}：{fd(r["dur"])} 的 case 花了 {fmt(r["aht"])}，比规律{"多" if e > 0 else "少"}'
              f' {fmt(abs(e))} —— 被打断了？'); warn += 1
    if len(rows) >= 10 and L['b'] != 0:
        worst = None
        for i in range(len(rows)):
            Li = fit_linear(x[:i] + x[i + 1:], y[:i] + y[i + 1:])
            ch = abs(Li['b'] - L['b']) / abs(L['b'])
            if worst is None or ch > worst[1]: worst = (rows[i], ch)
        if worst[1] > 0.3:
            print(f'  ⚠ 单独去掉 {worst[0]["id"]}，「{rate()}」就变 {worst[1] * 100:.0f}% —— '
                  f'结果被这一条左右，核实一下它'); warn += 1
    if not warn:
        print('  没发现问题')

    hr('③ 规律')
    r2 = L['r2']
    print(f'  {lbl()}能解释 AHT 差异的 {r2 * 100:.0f}%')
    if r2 < 0.3 and not a.force_linear:
        M = fit_flat(x, y)
        if narrow:
            print(f'  → 试标的{lbl()}跨度太窄，看不出长短的影响（不代表没影响）。')
            print(f'    先统一用平均值；要按{lbl()}算，得补做一些更短和更长的 case 再重跑')
        else:
            print(f'  → 太低了，{lbl()}跟花多少时间关系不大，按{lbl()}调目标没有意义。')
            print(f'    改为不分长短、统一用平均值。（确定要按{lbl()}算就加 --force-linear）')
    elif L['a'] < 0:
        M = fit_origin(x, y)
        print(f'  → 算出来的起步时间是负数（{L["a"]:.0f}s），实际不可能，样本少时常见。改用「完全按比例」')
    else:
        M = L
    explain(M)
    if M['mode'] != 'flat':
        print(f'\n  {lbl():>8}   每条该给')
        for d in sorted({round(dmin), round(median(x)), round(dmax)}):
            print(f'  {fd(d):>8}   {fmt(pred(M, d))}')

    # 生产时长分布
    if a.prod_durations:
        pr, _, _, _ = load(a.prod_durations, a.dur_col, need_aht=False)
        pdur = mean([r['dur'] for r in pr]); src = f'生产{lbl()}列表（{len(pr)} 条）'
        outside = sum(1 for r in pr if r['dur'] < dmin or r['dur'] > dmax)
    elif a.prod_mean:
        pdur, src, outside = a.prod_mean, f'你给的生产平均{lbl()}', 0
    else:
        pdur, src, outside = mean(x), f'本表的平均{lbl()}', 0

    level = '生产（标注员）' if a.vs else a.level
    hr('④ 基线')
    y0, lo, hi = pred_ci(M, pdur)
    print(f'  按{src} {fd(pdur)} 算')
    print(f'  基线 AHT   {fmt(y0)}（{y0:.0f}s）   95% 区间 {lo:.0f}–{hi:.0f}s')
    print(f'  取整到 10 秒：{ceil10(y0)}s')
    print(f'  这是「{level}」的水平')
    if not a.prod_durations and not a.prod_mean:
        print(f'  ⚠ 没给生产的{lbl()}，用的是本表的平均值。生产和本表长短分布不一样时，用 --prod-mean 或 --prod-durations')
    if pdur < dmin or pdur > dmax:
        print(f'  ⚠ 生产平均时长超出了本表范围（{fd(dmin)}–{fd(dmax)}），基线是外推的，不可靠')
    if outside:
        print(f'  ⚠ 生产里有 {outside} 条视频超出本表时长范围')

    # 对比项目经理
    ratio = None
    if a.vs:
        PM = json.load(open(a.vs, encoding='utf-8'))
        hr('⑤ 标注员 vs 项目经理')
        p = [max(pred(PM, r['dur']), 1) for r in rows]
        ratio = mean(y) / mean(p)
        rg = gmean([yi / pi for yi, pi in zip(y, p)])
        word = '慢' if ratio > 1 else '快'
        print(f'  同样长度的视频，标注员整体比项目经理{word} {abs(ratio - 1) * 100:.0f}%'
              f'（换算系数 {ratio:.2f}；按每条比值的几何平均是 {rg:.2f}）')
        print(f'  项目经理的基线要乘 {ratio:.2f} 才是标注员水平')
        print(f'  下个项目只有项目经理试标时，可以先用这个系数：试标基线 × {ratio:.2f}')
        outside_pm = sum(1 for r in rows if r['dur'] < PM['dmin'] or r['dur'] > PM['dmax'])
        if outside_pm:
            print(f'  ⚠ 生产里 {outside_pm} 条视频超出试标的时长范围，这部分的对比是外推的')

        if has_worker and M['mode'] != 'flat':
            hr('⑥ 各标注员（相对本次规律）')
            g = {}
            for r in rows: g.setdefault(r['worker'], []).append(r['aht'] / max(pred(M, r['dur']), 1))
            ws = sorted(((mean(v), k, len(v)) for k, v in g.items() if k), reverse=True)
            for m_, k, n_ in ws:
                print(f'  {k:>16}  n={n_:>3}  {m_:.2f}x' + ('' if n_ >= 5 else '  （样本少，仅供参考）'))
            solid = [m_ for m_, k, n_ in ws if n_ >= 5]
            if len(solid) >= 2:
                print(f'  样本 ≥5 条的人之间差 {max(solid) / min(solid):.1f} 倍 —— 目标适合排产，不适合直接考核个人')

    if is_trial:
        print(f'\n  ⚠ 提醒：这是项目经理一个人的水平。上线 2–3 天后用生产数据重跑一次：'
              f'\n     python3 calib.py fit 生产数据.csv --vs {a.save or "这次保存的模型.json"} --save prod.json')

    if a.save:
        keep = {k: v for k, v in M.items() if k != 'res'}
        keep.update({'dmin': dmin, 'dmax': dmax, 'level': level, 'source': a.data,
                     'created': datetime.date.today().isoformat(), 'baseline': y0, 'prod_mean': pdur,
                     'unit': UNIT})
        if ratio: keep['pm_ratio'] = ratio
        json.dump(keep, open(a.save, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        print(f'\n  模型已保存：{a.save}')
    print()


# ---------------- predict ----------------
def cmd_predict(a):
    global UNIT
    M = json.load(open(a.model, encoding='utf-8'))
    UNIT = M.get('unit', '秒')
    if a.mean is not None:
        durs, ids = [a.mean], None
    elif a.durations:
        rows, _, _, _ = load(a.durations, a.dur_col, need_aht=False)
        durs, ids = [r['dur'] for r in rows], [r['id'] for r in rows]
    else:
        sys.exit('需要 --mean 这批平均时长，或 --durations 这批的时长列表')

    dbar = mean(durs)
    y, lo, hi = pred_ci(M, dbar)
    y, lo, hi = y * a.ratio, lo * a.ratio, hi * a.ratio
    hr('本批 AHT 目标')
    print(f'  模型：{M["source"]}（{M["level"]}，{M["created"]}，{M["n"]} 条）')
    if a.ratio != 1:
        print(f'  已乘换算系数 {a.ratio:.2f}')
    print(f'  本批 {len(durs)} 条，平均{lbl()} {fd(dbar)}' if ids else f'  本批平均{lbl()} {fd(dbar)}')
    print(f'\n  目标 AHT   {fmt(y)}（{y:.0f}s）')
    print(f'  95% 区间   {lo:.0f}–{hi:.0f}s')
    print(f'  取整到 10 秒：{ceil10(y)}s')
    if M['mode'] == 'flat':
        print('  （这个模型不按时长调，所以跟本批视频长短无关）')

    out = [d for d in durs if d < M['dmin'] or d > M['dmax']]
    if dbar < M['dmin'] or dbar > M['dmax']:
        print(f'\n  ⚠ 本批平均时长超出了定标范围（{fd(M["dmin"])}–{fd(M["dmax"])}），目标是外推的，不可靠')
    elif out and ids:
        print(f'\n  ⚠ 本批有 {len(out)} 条（{len(out) / len(durs) * 100:.0f}%）视频超出定标范围'
              f'（{fd(M["dmin"])}–{fd(M["dmax"])}），最长 {fd(max(durs))}')
    if '项目经理' in M['level'] and a.ratio == 1:
        print(f'  ⚠ 这是项目经理水平的模型，还没有用生产数据校准')

    if a.per_case:
        if not ids: sys.exit('--per-case 需要配合 --durations 使用')
        with open(a.per_case, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f); w.writerow(['case_id', f'{lbl()}({UNIT})', '目标AHT(秒)'])
            for i, d in zip(ids, durs):
                w.writerow([i, f'{d:g}', f'{pred(M, d) * a.ratio:.0f}'])
        print(f'\n  逐条目标已写入：{a.per_case}')
    print()


def main():
    ap = argparse.ArgumentParser(description='按视频时长定 AHT 目标')
    sub = ap.add_subparsers(dest='cmd', required=True)

    f = sub.add_parser('fit', help='用逐条数据定标（试标或生产）')
    f.add_argument('data')
    f.add_argument('--warmup', type=int, default=0, help='去掉前 N 条热身数据')
    f.add_argument('--prod-mean', type=to_sec, help='生产视频的平均时长（秒或 mm:ss）')
    f.add_argument('--prod-durations', help='生产视频时长列表 CSV，用来算基线')
    f.add_argument('--vs', help='项目经理的模型 JSON；给了就算「项目经理 → 标注员」换算系数')
    f.add_argument('--level', default='项目经理试标', help='这份数据是谁做的（写进模型）')
    f.add_argument('--unit', default='秒', help='规模变量的单位：视频时长用默认的「秒」，其他如 segment')
    f.add_argument('--force-linear', action='store_true', help='时长解释力低时也强制按时长算')
    f.add_argument('--save', help='保存模型 JSON')
    f.add_argument('--dur-col'); f.add_argument('--aht-col')
    f.set_defaults(func=cmd_fit)

    p = sub.add_parser('predict', help='给新一批视频出目标')
    p.add_argument('--model', required=True)
    p.add_argument('--mean', type=to_sec, help='这批视频的平均时长（秒或 mm:ss）')
    p.add_argument('--durations', help='这批视频的时长列表 CSV')
    p.add_argument('--ratio', type=float, default=1.0, help='乘一个换算系数（如上个项目算出的 1.25）')
    p.add_argument('--per-case', help='逐条目标输出 CSV')
    p.add_argument('--dur-col')
    p.set_defaults(func=cmd_predict)

    a = ap.parse_args()
    a.func(a)


if __name__ == '__main__':
    main()
