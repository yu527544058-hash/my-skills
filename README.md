# my-skills

我自己用的 [Claude Code](https://claude.com/claude-code) 技能包。

> **Claude Code 是什么**：Anthropic 出的 AI 助手，可以在终端、桌面 App 或 VS Code 里用。
>
> **Skill（技能包）是什么**：给 Claude 装的"专项能力"。装好之后，你用中文跟 Claude 说需求，它就会按技能包里的方法来做，**不需要你自己写代码或跑命令**。

---

## aht-target：AHT 目标测算

**AHT** 是 Average Handling Time 的缩写，就是**平均每条 case 的处理时长**。

这个技能帮你：

- **根据数据定出 AHT 目标**，并随着 case 的构成变化灵活调整：长 case 变多了、视频变长了、字数变多了，目标都能跟着重新算
- **顺带告诉你**，处理时间的差异是 case 本身造成的，还是人造成的

适合给标注员、审核员、客服这类"按条计时"的岗位定时间目标的运营和项目经理。

### 装好以后怎么用

直接跟 Claude 说就行，比如：

- "长 case 占比从 7% 涨到 12%，AHT 该给多少？"
- "新项目是文本标注，项目经理试标了 30 条，帮我定个 AHT 基线"（然后把表格发给它）
- "帮我看看这批数据，AHT 差异是什么造成的"

第一次用，先输入 **`/aht-target help`**，会显示一份大白话的使用指南：三种用法、每种要准备什么数据、常见问题。

### 安装

**第一步：装 Claude Code。** 到 [Claude Code 官网](https://claude.com/claude-code) 下载，桌面 App、VS Code 插件、终端版都可以。

**第二步：装这个技能包。** 两种方式选一种：

- **不会用终端**：打开 Claude Code，把这个页面的链接发给它，说"帮我安装这个仓库里的 aht-target 技能"，它会帮你装好。注意要在 **Claude Code** 里发，网页版的聊天窗口没法把东西装到你的电脑上
- **自己装**：在终端里运行下面三行。装在用户目录下，所有项目都能用

```bash
git clone https://github.com/yu527544058-hash/my-skills.git
mkdir -p ~/.claude/skills
cp -r my-skills/aht-target ~/.claude/skills/
```

装完重新打开 Claude Code，输入 `/aht-target help`，能看到使用指南就说明装好了。

### 它是怎么算的（想了解原理再看）

**老项目：长 case 占比变了**

平均时长就是短 case 和长 case 按比例混在一起。举个例子：短 case 每条 500 秒，长 case 每条 1,500 秒，长 case 占 10%，平均就是 500 × 90% + 1,500 × 10% = 600 秒。长 case 占比每涨 1 个百分点，平均时长就多出 (1,500 − 500) ÷ 100 = 10 秒。

**新项目：还没有历史数据**

每条处理时间 = 起步时间 + 每单位多花 × 长短，就像打车费 = 起步价 + 每公里的钱。"长短"就是影响处理时间的主要因素：视频看时长，文本看字数，图片看张数。项目经理试标几十条，就能算出起步时间是多少、每单位多花多少。

**它会帮你避开的几个坑**

- 说"现在 AHT 是 500 秒"时，要先说清楚这 500 秒**包不包含**长 case。说反了，算出来连该加还是该减都会反
- "长 case"的标准要统一。占比按"超过 30 个 segment"算，长 case 的平均时长也得按同一个标准算
- 一条挂机 3 小时的记录，就能把平均值拉高一大截，要先挑出来
- 同一个 case 换个人做，时间经常差 2 倍。算出来的目标适合排产、估人力，不适合直接拿来考核个人

<details>
<summary><b>高级：自己在终端里跑脚本</b>（一般用不到，Claude 会替你跑）</summary>

两个脚本都只用 Python 自带的库，不用额外安装。

**`aht.py`：老项目，长 case 占比变了**

输入一个 CSV，里面**只放长 case** 的逐条明细。至少要有处理时长这一列；有标注员和 case ID 列时，还会分析人和人之间差多少。

```bash
python3 aht-target/scripts/aht.py 长case明细.csv --baseline 500 --baseline-is short --q 20%
```

- `--baseline 500`：现在的 AHT 基线，单位是秒
- `--baseline-is short`：这 500 秒是"完全没有长 case 时"的水平。如果是"现在所有 case 的平均"，写 `mixed`，并用 `--q0 10%` 说明当时长 case 占多少
- `--q 20%`：想算长 case 占比为多少时的目标。写 `20%` 或 `0.2` 都行，可以写多个
- `--t-long 1800`（可选）：长 case 的平均时间，单位秒。手上有更新的实测值时用它，代替文件里算出来的
- `--q-now 7%`（可选）：现在实际的长 case 占比。给了之后，能判断要算的占比是不是比现在高出太多
- `--tech`（可选）：多显示一些统计细节，给懂统计的人看

**`calib.py`：新项目，按长短定标**

```bash
# 1. 项目经理试标定基线
python3 aht-target/scripts/calib.py fit 试标.csv --warmup 4 --prod-mean 1200 --save pm.json
# 2. 正式开工 2–3 天后，用标注员的数据重新算
python3 aht-target/scripts/calib.py fit 生产.csv --vs pm.json --save prod.json
# 3. 以后每一批，按这批的平均长短出目标
python3 aht-target/scripts/calib.py predict --model prod.json --mean 900
```

- `--warmup 4`：前 4 条是热身，不计入
- `--prod-mean 1200`：正式开工后平均每条的长短（这个例子里是 1200 字）
- `--save pm.json`：把算出来的起步时间和每单位多花存成一个文件，后面的步骤要用它
- `--vs pm.json`：拿项目经理试标的结果来对比，算出标注员比项目经理慢/快多少
- `--model prod.json --mean 900`：用存下来的结果，算平均 900 字的这一批该给多少时间

列名写"视频时长""字数""图片数""segment"，会自动认出单位。

</details>

## License

MIT
