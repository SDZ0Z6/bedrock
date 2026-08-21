# 部署到阿里云 ECS（吉隆坡）

从一台全新的 ECS 开始，到 `https://你的域名` 能打开为止。

**目标环境**：阿里云 ECS · 吉隆坡（`ap-southeast-3`）· Ubuntu 24.04 LTS ·
Nginx 反向代理 · Let's Encrypt 证书自动续期 · 公网开放

**先读这一段**：这个平台的 `cred.xlsx` 里是两个 AWS 账号的 **AK/SK 明文**，而
登录只有一道口令。放到公网上等于「一道口令挡着两套云凭证」。所以本文档里的
第 8 章（加固）不是可选项，请全部做完。最小化那两把密钥的 IAM 权限（只给
只读的 CE / CloudWatch，见 README 的《AK/SK 需要什么权限》）是性价比最高的一步——真出事时
损失的上限就被它框住了。

---

## 0. 前置清单

| 项目 | 说明 |
|---|---|
| 阿里云账号 | 已实名认证 |
| 域名 | 已注册。**吉隆坡不在中国大陆，不需要 ICP 备案** |
| 本地环境 | Windows，装了 OpenSSH 客户端（Win10/11 自带，`ssh -V` 能出版本即可） |

---

## 1. 买 ECS

控制台 → 云服务器 ECS → 创建实例。

| 配置项 | 建议值 | 理由 |
|---|---|---|
| 地域 | 马来西亚（吉隆坡）`ap-southeast-3` | 你指定的 |
| 实例规格 | `ecs.e-c1m2.large`（2 核 4G）或同级 | 这个平台是低频内部看板，2C4G 富余。1C2G 也能跑，但 certbot 和构建时会紧张 |
| 镜像 | Ubuntu 24.04 64 位 | 自带 Python 3.12，满足本项目 `>=3.10` |
| 系统盘 | 40G ESSD | 系统 + 日志够用 |
| 公网 IP | 分配，按使用流量 | 看板流量极小，按量比固定带宽便宜 |
| 带宽峰值 | 5 Mbps | 够了 |
| 登录凭证 | **密钥对**，不要用密码 | 密码登录会被全网扫描器爆破 |

创建时会让你选**安全组**，先放行 22 即可，80/443 下一章再开。

> 记下实例的**公网 IP**，后面处处要用。下文一律写作 `<ECS_IP>`。

---

## 2. 安全组（阿里云的防火墙）

控制台 → ECS → 网络与安全 → 安全组 → 配置规则 → 入方向。

| 端口 | 授权对象 | 说明 |
|---|---|---|
| 22 | **你的出口 IP/32** | SSH。不要写 `0.0.0.0/0` |
| 80 | `0.0.0.0/0` | Let's Encrypt 验证 + HTTP 跳 HTTPS 用 |
| 443 | `0.0.0.0/0` | 平台本体 |

查自己的出口 IP：浏览器打开 <https://myip.ipip.net>。

> 家里宽带 IP 会变。变了之后 SSH 连不上，回控制台改这条规则即可（控制台走的是
> 阿里云自己的通道，不受安全组影响，不会把自己锁在外面）。

**安全组之外不需要再配 ufw**。两层防火墙容易互相打架、排查时多绕一层，
阿里云安全组已经在实例之外拦掉了，够用。

---

## 3. 域名解析

控制台 → 域名 → 解析设置 → 添加记录。

| 记录类型 | 主机记录 | 记录值 | TTL |
|---|---|---|---|
| A | `bedrock`（或 `@` 用裸域名） | `<ECS_IP>` | 10 分钟 |

假设最终域名是 `bedrock.example.com`，下文一律写作 `<DOMAIN>`。

等解析生效（通常 1–10 分钟），在本地验证：

```bash
nslookup bedrock.example.com
```

**必须先看到正确的 IP 再往下走**，第 7 章签证书要靠它。

---

## 4. 首次登录与系统准备

Windows PowerShell 里：

```bash
ssh -i C:\path\to\your-key.pem root@<ECS_IP>
```

> 如果提示密钥权限过于开放，在 PowerShell 里执行：
> `icacls your-key.pem /inheritance:r /grant:r "$($env:USERNAME):(R)"`

登录后，**以下命令都在服务器上执行**。

### 4.1 更新系统、装依赖

```bash
apt update && apt upgrade -y
```

```bash
apt install -y git python3-venv python3-pip nginx fail2ban
```

`git` 第 5 章拉代码用，`fail2ban` 第 8.4 章用。

### 4.2 建一个专用的非 root 用户跑服务

服务不该用 root 跑——被打穿时影响面差别很大。

```bash
adduser --system --group --no-create-home --home /opt/bedrock bedrock
```

`--system` 建的是不能登录的系统账号，正合适。`--no-create-home` 是因为下一章要用
`git clone` 生成 `/opt/bedrock`——目录先存在的话 clone 会拒绝。

---

## 5. 拉代码

代码在 GitHub 上，服务器直接 clone。`.env` 和 `cred.xlsx` 在 `.gitignore` 里，
**不会**被 clone 下来，要单独传——这正是我们想要的：密钥永远不进仓库。

### 5.1 clone

仓库是公开的，不需要任何凭证：

```bash
git clone https://github.com/SDZ0Z6/bedrock.git /opt/bedrock
```

<details>
<summary>如果以后把仓库改成私有</summary>

用 **Deploy Key**（只读、只对这一个仓库有效、可随时吊销），比 Personal Access
Token 安全得多——PAT 一泄露就是你整个 GitHub 账号。

服务器上生成一把密钥：

```bash
ssh-keygen -t ed25519 -C "bedrock-ecs-deploy" -f /root/.ssh/bedrock_deploy -N ""
```

```bash
cat /root/.ssh/bedrock_deploy.pub
```

把输出贴到 GitHub → 该仓库 → Settings → Deploy keys → Add deploy key，
**不要**勾 "Allow write access"。然后配置 SSH：

```bash
{ echo "Host github.com"; echo "  IdentityFile /root/.ssh/bedrock_deploy"; echo "  IdentitiesOnly yes"; } >> /root/.ssh/config
```

```bash
git clone git@github.com:SDZ0Z6/bedrock.git /opt/bedrock
```

</details>

阿里云吉隆坡访问 GitHub 是通的，不用配代理（如果 ECS 在中国大陆则另说）。

### 5.2 传台账

`cred.xlsx` 里是 AWS 密钥，永远不进仓库，**从你 Windows 本地**传：

```bash
scp -i C:\path\to\your-key.pem cred.xlsx root@<ECS_IP>:/opt/bedrock/
```

`.env` 不用传——第 6 章会在服务器上重新生成一份更严的（本地那份的口令是明文，
服务器上要用哈希）。

### 5.3 收紧权限

```bash
chown -R bedrock:bedrock /opt/bedrock
chmod 750 /opt/bedrock
chmod 640 /opt/bedrock/cred.xlsx
```

`cred.xlsx` 的 `640` 表示只有 `bedrock` 用户和同组能读。

代码归 `bedrock` 了，但你是用 root 跑 `git pull`。git 2.35 之后遇到「仓库属主
不是当前用户」会直接拒绝（dubious ownership），所以要显式声明这个目录可信，
否则第一次更新代码就会卡住：

```bash
git config --global --add safe.directory /opt/bedrock
```

> 想确认拉下来的代码是对的，可以跑一遍测试（第 6 章装完依赖之后）：
> `cd /opt/bedrock && .venv/bin/python -m pytest`，应该全过且不联网。

---

## 6. 装依赖、生成配置

### 6.1 虚拟环境

```bash
cd /opt/bedrock && python3 -m venv .venv
```

```bash
/opt/bedrock/.venv/bin/pip install --upgrade pip
```

```bash
/opt/bedrock/.venv/bin/pip install -e ".[deploy]"
```

`[deploy]` 会一并装上 gunicorn。

### 6.2 生成生产用的 `.env`

**口令用哈希，不用明文**。先生成哈希（把 `你的强密码` 换掉）：

```bash
/opt/bedrock/.venv/bin/python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('你的强密码'))"
```

再生成会话密钥：

```bash
/opt/bedrock/.venv/bin/python -c "import secrets; print(secrets.token_hex(32))"
```

然后写 `/opt/bedrock/.env`（`nano /opt/bedrock/.env`，把两个占位符换成上面的输出）：

```ini
# ---------------- 登录 ----------------
AUTH_USERNAME=admin
# 用哈希，不要用 AUTH_PASSWORD 明文；设了哈希后明文会被忽略
AUTH_PASSWORD_HASH=粘贴上面 generate_password_hash 的输出
SECRET_KEY=粘贴上面 token_hex 的输出
SESSION_HOURS=12
MAX_LOGIN_ATTEMPTS=5
LOCKOUT_SECONDS=900

# ---------------- 公网部署必须打开的两项 ----------------
# 会话 cookie 只在 HTTPS 下发送
SESSION_COOKIE_SECURE=true
# 让 Flask 通过 Nginx 转发的头还原真实客户端 IP 和协议。
# 不打开的话所有请求看起来都来自 127.0.0.1，按 IP 的登录锁定就废了。
TRUST_PROXY=true

# ---------------- 数据源 ----------------
EXCEL_PATH=cred.xlsx
TAG_KEY=map-migrated
COST_METRIC=UnblendedCost
SERVICE_FILTER=
CACHE_TTL=900

# ---------------- 展示 ----------------
CURRENCY_SYMBOL=$
WARN_PCT=70
DANGER_PCT=90

# ---------------- 运行 ----------------
# 只监听本机，公网流量一律走 Nginx
HOST=127.0.0.1
PORT=5000
DEBUG=false
```

收紧权限——这个文件里有会话密钥：

```bash
chown bedrock:bedrock /opt/bedrock/.env && chmod 600 /opt/bedrock/.env
```

### 6.3 冒烟测试

```bash
cd /opt/bedrock && sudo -u bedrock .venv/bin/python -c "
from bedrock_cost import config, create_app
print('项目根 :', config.BASE_DIR)
print('台账   :', config.EXCEL_PATH, config.EXCEL_PATH.is_file())
print('提醒   :', config.startup_warnings() or '无')
app = create_app()
print('路由   :', sorted(str(r) for r in app.url_map.iter_rules()))
print('secure cookie:', app.config['SESSION_COOKIE_SECURE'])
"
```

期望：台账为 `True`、提醒为「无」、`secure cookie: True`。

---

## 7. gunicorn + systemd

### 7.1 为什么是「1 个 worker + 多线程」

这一点别照抄网上的 `--workers 4`。本项目的**缓存和登录失败计数都在进程内存
里**：

- 多个 worker 各有一份缓存 → 缓存命中率被摊薄，Cost Explorer 按请求计费，等于
  多花钱
- 更要紧的是，登录失败计数也是每个 worker 一份 → 配 `MAX_LOGIN_ATTEMPTS=5` 开
  4 个 worker，实际要试 20 次才会锁

所以用**单进程 + 多线程**：缓存和计数天然共享，而这个平台大部分时间是在等
AWS 的 API 返回（IO 密集），线程完全够用。

### 7.2 systemd 服务

写 `/etc/systemd/system/bedrock.service`：

```ini
[Unit]
Description=Bedrock 成本监控平台
After=network.target

[Service]
Type=notify
User=bedrock
Group=bedrock
WorkingDirectory=/opt/bedrock
Environment=PYTHONUNBUFFERED=1
# 单 worker + 多线程：缓存与登录锁定是进程内状态，多 worker 会把两者都摊薄
ExecStart=/opt/bedrock/.venv/bin/gunicorn \
    --workers 1 \
    --threads 8 \
    --bind 127.0.0.1:5000 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    "bedrock_cost:create_app()"
Restart=always
RestartSec=5

# 加固：能不给的权限就不给
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/bedrock
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
```

> `--timeout 120`：查 CloudWatch 四个区 × 多个账号，冷缓存时可能要几十秒，
> 默认的 30 秒会被杀掉。

启动：

```bash
systemctl daemon-reload && systemctl enable --now bedrock
```

```bash
systemctl status bedrock --no-pager
```

本机验证（此时还没有 Nginx）：

```bash
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5000/login
```

期望 `200`。

---

## 8. Nginx + HTTPS + 加固

### 8.1 先配 HTTP（certbot 需要）

写 `/etc/nginx/sites-available/bedrock`（把 `<DOMAIN>` 换成你的域名）：

```nginx
# 登录接口限速：10r/m 表示每个 IP 每分钟 10 次
limit_req_zone $binary_remote_addr zone=bedrock_login:10m rate=10r/m;

server {
    listen 80;
    server_name <DOMAIN>;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        # 覆写而不是追加：客户端自己塞的 X-Forwarded-For 不能被信任
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

启用并重载：

```bash
ln -sf /etc/nginx/sites-available/bedrock /etc/nginx/sites-enabled/bedrock && rm -f /etc/nginx/sites-enabled/default && nginx -t && systemctl reload nginx
```

此时 `http://<DOMAIN>` 应该能打开登录页了。**打不开就别往下走**，先查解析和安全组。

### 8.2 签证书

```bash
apt install -y certbot python3-certbot-nginx
```

```bash
certbot --nginx -d <DOMAIN> --agree-tos -m 你的邮箱@example.com --redirect
```

`--redirect` 会自动加上 HTTP → HTTPS 跳转。certbot 会自己改上面那个配置文件。

验证自动续期（只演练不真签）：

```bash
certbot renew --dry-run
```

certbot 装好时会自带一个 systemd timer，不需要你再配 cron：

```bash
systemctl list-timers | grep certbot
```

### 8.3 补上加固配置

certbot 改完之后，再编辑 `/etc/nginx/sites-available/bedrock`，在 `listen 443`
那个 server 块的 `location /` **之前**插入这几段：

```nginx
    # HSTS：让浏览器以后一律直接走 HTTPS
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;

    # 不暴露 Nginx 版本号
    server_tokens off;

    # 登录接口限速，挡住暴力破解。burst=5 允许短时突发，nodelay 不排队直接拒
    location = /login {
        limit_req zone=bedrock_login burst=5 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
    }
```

重载：

```bash
nginx -t && systemctl reload nginx
```

### 8.4 fail2ban 封爆破 IP

应用在登录失败时返回 **401**，所以可以直接从 Nginx 访问日志里识别。

写 `/etc/fail2ban/filter.d/bedrock-login.conf`：

```ini
[Definition]
failregex = ^<HOST> .* "POST /login HTTP/[0-9.]+" 401
ignoreregex =
```

写 `/etc/fail2ban/jail.d/bedrock.conf`：

```ini
[bedrock-login]
enabled  = true
port     = http,https
filter   = bedrock-login
logpath  = /var/log/nginx/access.log
maxretry = 10
findtime = 600
bantime  = 3600
```

```bash
systemctl enable --now fail2ban && fail2ban-client status bedrock-login
```

> 这是**第三道**防线：应用自己的 IP 锁定（5 次/15 分钟）→ Nginx 限速
> （10 次/分钟）→ fail2ban 封 IP（10 次/10 分钟封 1 小时）。

### 8.5 验收

```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://<DOMAIN>/login
```

```bash
curl -sS -I http://<DOMAIN> | head -3
```

期望：第一条 `200`；第二条是 `301`，`Location` 指向 `https://`。

浏览器打开 `https://<DOMAIN>`，用第 6.2 章设的口令登录，三个页面都点一遍。

---

## 9. 日常运维

### 看日志

```bash
journalctl -u bedrock -f
```

```bash
tail -f /var/log/nginx/access.log
```

### 重启 / 停止

```bash
systemctl restart bedrock
```

### 更新代码

本地 `git push` 之后，在服务器上：

```bash
cd /opt/bedrock && git pull
```

```bash
chown -R bedrock:bedrock /opt/bedrock && systemctl restart bedrock
```

`git pull` **不会碰 `.env` 和 `cred.xlsx`**——它们在 `.gitignore` 里，git 根本不
管它们，所以更新代码永远不会覆盖掉你的配置和台账。

依赖有变动时（`pyproject.toml` 改了），重启前先补一句：

```bash
cd /opt/bedrock && .venv/bin/pip install -e ".[deploy]"
```

出问题想回退到上一个版本：

```bash
cd /opt/bedrock && git log --oneline | head -5
```

```bash
cd /opt/bedrock && git checkout <上一个提交号> && systemctl restart bedrock
```

回到最新：`git checkout master && git pull`。

### 更新台账（加账号 / 改预算）

> **从这一版开始，日常增删改账号请直接用页面上的「账号管理」，不要再 scp 覆盖。**
> 两个入口同时用会互相冲掉：你本地那份 `cred.xlsx` 不含页面上做过的改动，一传
> 上去就把它们全抹了。下面的 scp 流程只在**首次部署**和**从备份恢复**时用。
>
> 页面写入需要 `bedrock` 用户对 `/opt/bedrock` 目录有写权限（原子替换要在同目录
> 建临时文件），systemd 单元里的 `ReadWritePaths=/opt/bedrock` 已经满足；另外它
> 会在那里建 `ledger-backups/` 和 `ledger-audit.log`：
>
> ```bash
> ls -l /opt/bedrock/ledger-backups/ && tail /opt/bedrock/ledger-audit.log
> ```
>
> `ledger-backups/` 里是**明文 AK/SK 的完整副本**，和 `cred.xlsx` 同等敏感，
> 备份服务器时别漏了它，公开分享时更别带上。

直接传新的 `cred.xlsx` 覆盖即可，**不用重启**——应用按文件的 mtime 判断，
刷新页面就生效：

```bash
scp -i C:\path\to\your-key.pem cred.xlsx root@<ECS_IP>:/opt/bedrock/
```

传完记得把属主改回来：

```bash
chown bedrock:bedrock /opt/bedrock/cred.xlsx && chmod 640 /opt/bedrock/cred.xlsx
```

### 备份

要备的只有两个文件，都不在 git 里：

```bash
scp -i C:\path\to\your-key.pem root@<ECS_IP>:/opt/bedrock/.env ./backup-env-$(date +%F)
```

```bash
scp -i C:\path\to\your-key.pem root@<ECS_IP>:/opt/bedrock/cred.xlsx ./backup-cred-$(date +%F).xlsx
```

---

## 10. 排错

| 现象 | 多半是什么 |
|---|---|
| 域名打不开，`curl` 超时 | 安全组没放行 80/443；或 DNS 还没生效（`nslookup` 确认） |
| 502 Bad Gateway | 应用没起来。`systemctl status bedrock` 和 `journalctl -u bedrock -n 50` |
| 登录页能开，但输对口令也进不去 | `SESSION_COOKIE_SECURE=true` 却在用 http 访问。走 https，或临时改成 false 排查 |
| 一直提示「登录失败次数过多」 | 触发了锁定。等 `LOCKOUT_SECONDS`，或 `systemctl restart bedrock` 清掉内存计数 |
| 页面显示「找不到账号台账文件」 | `cred.xlsx` 没传上去，或属主/权限不对（应为 `bedrock:bedrock 640`） |
| CloudWatch 页面空白但成本页正常 | AK/SK 缺 CloudWatch 权限，看 README 的《AK/SK 需要什么权限》 |
| certbot 签发失败 | 80 端口不通，或 DNS 没指到这台机器。certbot 需要从公网回访 80 |
| 改了 `.env` 不生效 | `.env` 是启动时读的，改完要 `systemctl restart bedrock` |
| `git pull` 报 local changes 冲突 | 服务器上不该改代码。`git checkout -- .` 丢弃本地改动后再拉（不会碰 `.env` / `cred.xlsx`） |
| `git clone` 提示目录已存在 | `/opt/bedrock` 已经有内容。建用户时要加 `--no-create-home`，见 4.2 |
| `git pull` 报 detected dubious ownership | 代码归 `bedrock` 而你用 root 跑 git。执行 `git config --global --add safe.directory /opt/bedrock`，见 5.3 |

---

## 附：这次为部署改了哪些代码

文档里用到的几项在原来的代码里不存在，一并补上了（都有测试覆盖）：

| 改动 | 为什么 |
|---|---|
| `SESSION_COOKIE_SECURE` 配置项 | 让会话 cookie 只在 HTTPS 下发送。默认关，本地 http 才登得进去 |
| `TRUST_PROXY` + ProxyFix | 还原真实客户端 IP。不加的话所有请求都来自 127.0.0.1，按 IP 的锁定完全失效 |
| `client_ip()` 不再直接读 `X-Forwarded-For` | 那个头客户端可以伪造，每次换一个假值就能绕开锁定。现在取 ProxyFix 还原后的 `remote_addr`，只认 Nginx 追加的那一个 |
| 登录失败返回 401（原来是 200） | 让 Nginx 日志里的失败登录可识别，fail2ban 才有的可封 |
| `pyproject.toml` 加 `deploy` extra | `pip install -e ".[deploy]"` 一步装上 gunicorn |
