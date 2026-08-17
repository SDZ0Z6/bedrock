# Bedrock 成本监控平台

用 Python + Flask 搭的轻量看板，把 `cred.xlsx` 里的账号台账和 AWS Cost Explorer 的实际消费拼在一起，显示每个上游账号的预算使用情况。

## 快速开始

最省事的方式是双击 `run.bat`（它自己设好 `PYTHONPATH`，不用装包）。

想按标准方式来：

```bash
python -m pip install -e ".[dev]"
```

然后启动：

```bash
python -m bedrock_cost
```

浏览器打开 <http://127.0.0.1:5000>，用 `.env` 里的 `AUTH_USERNAME` / `AUTH_PASSWORD` 登录。

> 装包时会生成一个 `bedrock-cost` 命令，但 Windows 应用商店版 Python 的用户级
> `Scripts` 目录默认不在 PATH 上，所以直接敲 `bedrock-cost` 可能找不到。
> `python -m bedrock_cost` 在哪种装法下都能用，推荐用它。

左侧导航有两个页面：

| 页面 | 路径 | 用途 |
|---|---|---|
| 概览 | `/` | 每个上游账号的预算 / 消费 / 使用率 / 余额一览 |
| 成本和使用情况 | `/cost-usage` | 按日或按月下钻，按服务 / 标签 / 账号维度看堆叠图和明细 |

侧边栏可以点左上角的箭头收起成一条图标轨（60px）。收起后三个入口（概览、
成本和使用情况、退出）都只剩图标，尺寸完全一致，悬浮可以看到 `title` 里的名字。
收起状态存在 `localStorage`，跨页面和重启浏览器都保持；`<head>` 里有一小段内联
脚本在首次绘制前就把状态套上，所以不会出现「先展开再收起」的闪动。窄屏
（≤900px）侧边栏本来就是横条，收起按钮会隐藏。

## 概览页字段口径

| 列 | 来源 |
|---|---|
| 上游 | Excel `PARTNER` |
| 账号 | Excel `ACCOUNT` |
| 预算 | Excel `BUDGET` |
| TAG 消费 | Cost Explorer 中**匹配 Excel `TAG`** 的消费 × Excel `TAG_RATIO` |
| UNTAG 消费 | Cost Explorer 中**其余全部**消费 × Excel `UNTAG_RATIO` |
| 总消费 | TAG 消费 + UNTAG 消费 |
| 使用率 | 总消费 ÷ 预算 × 100（预算为 0 时显示 `—`） |
| 余额 | 预算 − 总消费 |

TAG / UNTAG 单元格下方会显示 `CE 原始金额 × 比率` 的算式，账号下方会显示这一行用的标签依据，方便随时核对。

### TAG 列怎么写

`TAG` 列决定这个账号怎么拆分，格式是 `标签键=标签值`（`:` 和 `$` 也认）：

| TAG 列内容 | TAG 消费 | UNTAG 消费 |
|---|---|---|
| `map-migrated=migXYT8EVQSVP` | 该标签**等于这个值**的消费 | 其余全部（空值 + 其他值） |
| `map-migrated`（只给键） | 该标签**任意非空值**的消费 | 标签为空/缺失的消费 |
| 留空 / 没有 `TAG` 列 | 同上，标签键取 `.env` 的 `TAG_KEY` | 同上 |

两种口径下 **TAG + UNTAG 恒等于账号总消费**，不会有消费被漏掉。每个账号可以用不同的标签键和值。

> 写死标签值时，如果该区间内一分钱都没匹配上，表格里会出现一条黄色告警行，并列出 CE 中实际出现过的标签值。
> 这个提示很重要：MAP ID 打错一个字符，全部消费会被按 `UNTAG_RATIO` 加价，金额悄悄虚高。

顶部日期范围支持手动选择起止日期，也有「本月 / 上月 / 近 7 天 / 近 30 天 / 今年」快捷按钮。Cost Explorer 只保留约 14 个月历史数据，更早的开始日期会被自动收窄并提示。

## 成本和使用情况页

四个参数在页面顶部一行里，作用于本页全部图表和表格：

| 参数 | 取值 |
|---|---|
| 账号 | 全部账号（汇总）或单个账号 |
| 时间范围 | 起止日期，或本月 / 上月 / 近 7 天 / 近 30 天 / 今年 |
| 维度 | 服务（Bedrock 下等于按模型）/ 标签 / 账号 |
| 粒度 | 按日 / 按月 |

**任何一项改动都会立即重新查询**，所以这一页没有「查询」和「强制刷新」按钮。
提交时当前内容会压暗而不是闪骨架屏，避免布局跳动。要绕过缓存重新拉数据，用
页脚的「清空缓存」。（概览页保留了这两个按钮。）

快捷区间的高亮是拿解析出来的日期区间反查的，不依赖 URL 里的 `preset` 参数——
所以选完「近 30 天」再改维度，高亮不会掉。

**两个金额口径都在页面上**：

- **加价后** —— 逐格按标签归属乘 `TAG_RATIO` 或 `UNTAG_RATIO`。图表和明细表的日期列都用这个口径，各维度加总后与概览页的总消费**精确相等**。
- **原始 CE 金额** —— Cost Explorer 原样返回，与 AWS 控制台一致。汇总卡片和明细表各有一列。

之所以能做到逐格加权：服务维度用两级 `GroupBy [SERVICE, TAG]`（CE 上限就是两级），所以每一格都知道自己该乘哪个比率，一个账号仍然只发一次 API 请求。

### 图表

堆叠柱状图是手写 SVG，没有引入任何前端库或 CDN，离线可用。

- 配色用 dataviz 规范的深色 8 色分类板，已用该规范自带的 `validate_palette.py`
  对本项目卡片底色 `#1a1e2a` 跑过全部检查（亮度带 / 色度下限 / CVD 分离度 /
  常视觉下限 / 对比度全部 PASS，最差相邻 CVD ΔE 8.4、常视觉 ΔE 19.3）。
  验证脚本属于 dataviz 规范，不在本仓库里。**改颜色请重新跑一遍验证**：
  ```bash
  python validate_palette.py "#3987e5,#d95926,#199e70,#c98500,#d55181,#008300,#9085e9,#e66767" --mode dark --surface "#1a1e2a"
  ```
  `tests/test_chart.py` 里另有几条约束会一直守着：8 个色槽、「其他」不占分类色、
  色槽按固定顺序取用不生成第 9 个色。
- 序列超过 8 条时，第 9 条起按金额折叠进「其他」（中性灰，不占分类色槽），图例会写明折叠了几项。
- 颜色按序列名的摘要分配，不按金额排名。所以**金额涨跌导致的排序变化永远不会换色**；只要出现的序列还是那几个，改日期范围颜色也完全不变。某条序列彻底消失时，原先和它撞色槽的那一条会挪位——8 个色槽装 8 条序列时这无法避免，权衡的是「同一张图里不出现重复颜色」，那个更影响判读。
- 悬浮或键盘 Tab 到某一列会显示当天各序列的金额；这只是增强，同样的数值在下方明细表里都查得到。

## Cost Explorer 查询口径

- **指标**：`UnblendedCost`（可在 `.env` 改成 `AmortizedCost` / `NetUnblendedCost`）
- **服务范围**：账号下全部服务，不加服务过滤
- **拆分方式**：按 `TAG` 列里的标签键做 `GroupBy`，再在本地按标签值归类。一个账号一次 API 调用就能同时拿到 TAG 和 UNTAG 两部分。
- **端点**：`us-east-1`（Cost Explorer 是全局服务）
- **凭证**：每个账号用台账里自己的 `AK` / `SK`，只需要 `ce:GetCostAndUsage` 权限

> ⚠️ 注意：Bedrock 上的 Claude 在 Cost Explorer 里是**按模型独立计费条目**，服务名形如
> `Claude Opus 5 (Amazon Bedrock Edition)`，**不叫** `Amazon Bedrock`。
> 所以如果要启用 `SERVICE_FILTER`，必须填这些模型级的服务全名，否则会查出 0。

## 缓存

Cost Explorer 按请求计费（约 0.01 USD/次），结果按「账号 + 日期区间 + 指标」缓存 15 分钟（`.env` 里的 `CACHE_TTL`，单位秒，设 0 关闭）。

- 点「强制刷新」跳过缓存重新查一次
- 页脚「清空缓存」清掉全部缓存
- 改完 `cred.xlsx` 直接刷新页面即可生效，不用重启服务

## 增加账号

直接在 `cred.xlsx` 里追加一行就行。表头大小写、前后空格、**列顺序**都不敏感，多余的列会被忽略。

```
必需：PARTNER  ACCOUNT  BUDGET  TAG_RATIO  UNTAG_RATIO  AK  SK
可选：TAG
```

单个账号查询失败（凭证错误、权限不足等）只会让那一行显示错误，不影响其他账号；合计行只统计查询成功的账号。

## 配置项

全部集中在 `.env`（模板见 `.env.example`）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AUTH_USERNAME` / `AUTH_PASSWORD` | `admin` / 随机生成 | 登录口令 |
| `AUTH_PASSWORD_HASH` | 空 | 设置后改用 werkzeug 哈希校验，忽略 `AUTH_PASSWORD` |
| `SECRET_KEY` | 随机生成 | Flask 会话签名密钥，改动会让已登录会话失效 |
| `SESSION_HOURS` | `12` | 登录有效期 |
| `EXCEL_PATH` | `cred.xlsx` | 台账路径 |
| `TAG_KEY` | `map-migrated` | 兜底标签键，仅在台账 `TAG` 列留空/缺失时生效 |
| `COST_METRIC` | `UnblendedCost` | CE 成本指标 |
| `SERVICE_FILTER` | 空（全部服务） | 逗号分隔的 CE 服务全名 |
| `CACHE_TTL` | `900` | 缓存秒数 |
| `WARN_PCT` / `DANGER_PCT` | `70` / `90` | 使用率变黄 / 变红的阈值 |
| `HOST` / `PORT` | `127.0.0.1` / `5000` | 监听地址 |

## 安全须知

- `cred.xlsx` 里的 AK/SK 是明文。这些密钥只在内存中传给 boto3，**不会**出现在页面、日志或错误信息里（`Account` 的 `repr` 屏蔽了这两个字段，CE 报错文本也会做脱敏）。
- `.env` 和 `cred.xlsx` 已在 `.gitignore` 里，不要提交或分享。
- 给这些 AK/SK 配最小权限即可：
  ```json
  {
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["ce:GetCostAndUsage", "ce:GetTags"],
      "Resource": "*"
    }]
  }
  ```
- 默认只监听 `127.0.0.1`。要对外提供访问，请用 gunicorn/waitress + Nginx 并启用 HTTPS，同时把 `SESSION_COOKIE_SECURE` 打开——当前的登录机制（单账号 + 内存级失败锁定）是按本地自用设计的。

## 文件结构

标准的 src 布局：代码在 `src/bedrock_cost/`，配置和数据（`.env`、`cred.xlsx`）
留在项目根。

```
pyproject.toml            依赖、打包、pytest 配置（依赖的唯一来源）
requirements.txt          只写 `-e .`，让 pip install -r 仍然可用
run.bat                   Windows 一键启动（自设 PYTHONPATH，免安装）
.env / .env.example       登录口令、会话密钥、可调项
cred.xlsx                 账号台账（含 AK/SK，已在 .gitignore 里）

src/bedrock_cost/
  __init__.py             应用工厂 create_app()
  __main__.py             python -m bedrock_cost 入口
  config.py               .env 读取、默认值、项目根定位
  dates.py                日期区间解析与快捷项（不依赖 Flask，可单独测）
  auth.py                 登录蓝图：口令校验、失败锁定、login_required
  views.py                页面蓝图：概览 + 成本和使用情况
  filters.py              Jinja 过滤器（money / pct / ratio）与全局
  excel_source.py         读台账，解析 TAG 列，按 mtime 缓存
  cost_explorer.py        概览页：调 CE 并按标签拆 TAG/UNTAG，带 TTL 缓存与并发
  usage_explorer.py       下钻页：时间序列 + 三种维度 + 颜色槽分配
  report.py               概览页八列口径与合计
  chart.py                堆叠柱状图（SVG），图表色板的唯一来源
  templates/              base / shell / login / index / cost_usage
  static/style.css        深色仪表盘样式

tests/                    见下节
```

几条约定：

- **应用工厂**：没有模块级的全局 `app`，一律 `create_app()`。测试可以按需造带
  不同配置的实例，也不会在 import 时就产生副作用。
- **蓝图**：`auth`（登录）和 `main`（页面）两个，所以模板里的 endpoint 是
  `main.index`、`auth.logout` 这种带前缀的写法。
- **日期逻辑不碰 request**：`resolve_range(args, today)` 收一个普通映射，视图
  里传 `request.args`，测试里传字典，不用假造请求上下文。
- **图表色板只有一处定义**：都在 `chart.py`，模板里图例和表格的色块通过 Jinja
  全局 `series_color()` 取同一套值，CSS 里不重复维护。有个测试专门断言页面上
  出现的颜色都在色板内。
- **跨模块共用的函数不带下划线**：`cost_explorer.build_filter` 等是给
  `usage_explorer` 用的，所以是公开名。

## 测试

```bash
pytest
```

165 个用例，约 3 秒跑完，**不联网、不花钱**：Cost Explorer 调用被替换成 fake，
台账指向临时 xlsx，登录口令也换成固定值（不依赖你机器上的 `.env`）。

概览页和下钻页的 fake 都从同两个常量算消费，所以「下钻任一维度加总 == 概览总
消费」这条不变量测出来才有意义，不会因为两边各造一套数字而虚假通过。

真实调用 CE 的用例在 `tests/test_integration.py`，标了 `integration`，默认跳过：

```bash
pytest -m integration
```

跑它需要项目根有真实的 `cred.xlsx`。**注意 CE 按请求计费**（约 0.01 USD/次），
一轮下来是 账号数 × 用例数 次请求。它验证的是 fake 测不到的东西：标签拆分与
账号总额精确相等、按月查询在区间从月中开始时不错桶、缓存真的省掉一次请求、
无效凭证的报错不含密钥原文。
