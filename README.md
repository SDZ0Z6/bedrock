# Bedrock 成本监控平台

用 Python + Flask 搭的轻量看板，把 `cred.xlsx` 里的账号台账和 AWS Cost Explorer 的实际消费拼在一起，显示每个上游账号的预算使用情况。

## 快速开始

```bash
python -m pip install -r requirements.txt
```

然后双击 `run.bat`，或者：

```bash
python app.py
```

浏览器打开 <http://127.0.0.1:5000>，用 `.env` 里的 `AUTH_USERNAME` / `AUTH_PASSWORD` 登录。

## 主页字段口径

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

```
app.py             Flask 入口：路由、登录、日期区间解析、模板过滤器
config.py          .env 读取与默认值
excel_source.py    读 cred.xlsx，按 mtime 缓存
cost_explorer.py   调 CE 并按标签拆分 TAG/UNTAG，带 TTL 缓存与并发
report.py          主页表格的业务口径与合计
templates/         base / login / index
static/style.css   深色仪表盘样式
```
