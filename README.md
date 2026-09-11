# my-skills

给 [Claude Code](https://claude.com/claude-code) 用的 Skill 集合。

## 安装

放到用户级目录，任何项目下都能触发：

```bash
git clone https://github.com/yu527544058-hash/my-skills.git
mkdir -p ~/.claude/skills
cp -r my-skills/aht-target ~/.claude/skills/
```

只想在某个项目里生效就放到该项目的 `.claude/skills/` 下。

装好后在 Claude Code 里输入 `/aht-target help`，会显示一份大白话的使用指南（三种用法、要准备什么数据、常见问题）。

## Skills

### `aht-target` — 处理时长（AHT）目标测算

根据数据定出 AHT 目标，并随着 case 的构成变化灵活调整：贵 case 占比变了、视频变长了、字数变多了，目标都能跟着重新算。顺带诊断处理时间的差异是 case 本身造成的，还是人造成的。

适用于标注、客服工单、内容审核、质检、外包计件等任何**"按混合比例调整均值目标"**的场景。

**核心模型**（是算术，不是拟合）：

```
目标 = T_便宜 + q × (T_贵 − T_便宜)        斜率 = (T_贵 − T_便宜)/100 秒/百分点
```

**方法论覆盖 8 个环节**，每一个都对应一类真实踩过的坑：

| | 环节 | 要点 |
|---|---|---|
| 0 | 口径先行 | 阈值 / 分母 / 基线语义不钉死，后面全白算 |
| 1 | 校验转录 | 复算均值对汇总行；截图压缩太狠不要逐行猜数字 |
| 2 | 基线语义 | 混合均值 vs 无贵 case 基线 —— **搞反答案方向都是反的** |
| 3 | 混合均值模型 | T_贵 用均值不用中位数（只有均值满足可加性） |
| 4 | 找配对 | 同一 Item 被多人做过 = 天然对照实验，价值最高的一步 |
| 5 | 别急着说"无关" | R² 低可能是混杂因素淹了信号 |
| 6 | 外推检验 | T_贵 会随占比改变，不能拿旧值线性外推 |
| 7 | 数据质量 | 离群点影响、缺失值方向性、删失数据 |
| 8 | 量级对比 | 产能规划值 ≠ 个人考核线 |

**附带脚本** `aht-target/scripts/aht.py`：纯标准库，无依赖。一条命令跑完配对检验、方差分解、离群点影响、目标曲线。

```bash
python3 aht-target/scripts/aht.py data.csv \
        --baseline 500 --baseline-is short --q 0.10 --q 0.20
```

CSV 至少要有时长列；再有处理人和 Item ID 就能做配对分析。列名自动识别（中英文都支持），`deferred` / `In check` 等非数值会自动剔除并单独统计缺失的方向性。

**按规模定标** `aht-target/scripts/calib.py`：新项目还没有历史数据时用。规模就是"什么越多越费时"：视频看时长，文本看字数，图片看张数。列名写"视频时长""字数""图片数"会自动识别单位。

> 每条处理时间 = 起步时间 + 每单位多花 × 规模（就像打车费 = 起步价 + 每公里的钱）

```bash
# 1. 试标定基线（去掉前 4 条热身，生产时平均规模 75）
python3 aht-target/scripts/calib.py fit trial.csv --warmup 4 --prod-mean 75 --save pm.json
# 2. 上线 2–3 天后用生产数据重标，顺带算出「试标人 → 标注员」换算系数
python3 aht-target/scripts/calib.py fit prod.csv --vs pm.json --save prod.json
# 3. 每批按平均规模出目标
python3 aht-target/scripts/calib.py predict --model prod.json --mean 82
```

会自动检测热身期、离群值、试标规模跨度是否太窄；规模解释不了多少差异时，会改为统一用平均值而不硬套公式。

## License

MIT
