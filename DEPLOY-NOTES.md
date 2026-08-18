# 部署实录：Amazon Linux 2023 与 Ubuntu

这份文档记录 2026-08 把平台部署到 **AWS EC2（ap-southeast-5，吉隆坡）** 的实际过程，
以及路上踩到的每一个问题。

和 [DEPLOY.md](DEPLOY.md) 的分工：

- `DEPLOY.md` 是**一步步照做的完整流程**，写的是阿里云 ECS + Ubuntu（`apt`）。
- 这份文档只写**发行版差异**和**问题排查**，能照抄的章节直接指过去，不重复。

---

## 0. 先分清两条运行路径

本次最大的时间浪费来自没分清这两条路径。它们的监听地址来自**完全不同的地方**：

| | 启动方式 | 监听地址来自 | 适用场景 |
|---|---|---|---|
| **A. 开发服务器** | `python -m bedrock_cost` | `.env` 里的 `HOST` / `PORT` | 本机开发、临时验证 |
| **B. 生产** | gunicorn（systemd 拉起） | systemd 单元里的 `--bind` | 长期运行 |

- 路径 A 走 [`__main__.py`](src/bedrock_cost/__main__.py)，它把 `config.HOST` 传给 `app.run(host=...)`。
- 路径 B 走 `create_app()` 工厂，**根本不读 `config.HOST`**。整个代码库里 `HOST` 只在
  `__main__.py` 被用到一次。

> **推论**：跑 gunicorn 时改 `.env` 的 `HOST` 是无效操作，要改 systemd 单元里的 `--bind`。
> 反过来，走 `python -m` 时改 `--bind` 也没有意义（那时候没有 systemd 单元）。

另外，`.env` 的解析用的是 `os.environ.setdefault`（见 [config.py](src/bedrock_cost/config.py) 的
`_load_env_file`），**已存在的真实环境变量优先**。所以 systemd 单元里写 `Environment=XXX=...`
会盖掉 `.env` 里的同名项 —— 排查「配置改了不生效」时记得看一眼单元文件。

### 三个开关要成套改

`HOST`（或 `--bind`）、`SESSION_COOKIE_SECURE`、`TRUST_PROXY` 必须按场景成套配，
改错一个就会出「页面打不开」或「密码对却登不进去」：

| 场景 | HOST / --bind | SESSION_COOKIE_SECURE | TRUST_PROXY |
|---|---|---|---|
| 本机开发 | `127.0.0.1` | `false` | `false` |
| 裸 IP 临时测试（无 nginx） | `0.0.0.0` | `false` | `false` |
| nginx 反代，HTTPS 还没配好 | `127.0.0.1` | `false` | `true` |
| nginx + HTTPS（生产） | `127.0.0.1` | `true` | `true` |

- `SESSION_COOKIE_SECURE=true` 时浏览器**只在 HTTPS 下才保存会话 cookie**，纯 http 访问的
  表现是「密码正确但一直弹回登录页」。
- `TRUST_PROXY` 只有在**确实有 nginx 覆写 `X-Forwarded-For`** 时才能开。没有代理还开着，
  等于让客户端自己上报 IP，按 IP 的登录锁定形同虚设。
- `DEBUG` 在任何对外可达的场景下都必须是 `false`。`DEBUG=true` + 绑 `0.0.0.0` 会把
  Werkzeug 调试器暴露到公网，那是可以远程执行代码的。

---

## 1. 发行版命令对照

| 事项 | Ubuntu / Debian（DEPLOY.md 用的） | Amazon Linux 2023 |
|---|---|---|
| 包管理 | `apt update && apt upgrade -y` | `dnf update -y` |
| 基础包 | `apt install -y git python3-venv python3-pip nginx fail2ban` | `dnf install -y git nginx`（python 见 2.3，fail2ban 见问题 9） |
| 默认 python3 | 22.04 是 3.10、24.04 是 3.12，够用 | **3.9，不够用**，必须另装 |
| venv 模块 | 要单独装 `python3-venv` | 随 `python3.x` 包自带 |
| 建系统用户 | `adduser --system --group ...` | `useradd --system ...`（Debian 的 `adduser` 不存在） |
| 登录用户 | `root` 或 `ubuntu` | `ec2-user`，命令都要加 `sudo` |
| nginx 配置目录 | `sites-available` + `sites-enabled` 软链 | 只有 `/etc/nginx/conf.d/*.conf`，不用软链 |
| nginx 默认站点 | `sites-enabled/default`，要删 | 写在 `nginx.conf` 里的 `default_server` 块 |
| 主机防火墙 | `ufw` | 默认没装 firewalld，也没有 ufw —— **防火墙就是安全组** |
| SELinux | 无 | 默认 `permissive`（见问题 10） |
| certbot | `apt install certbot python3-certbot-nginx` | 仓库里可能没有（见问题 8） |
| fail2ban | 仓库里有 | 默认仓库没有，且 AL2023 不支持 EPEL（见问题 9） |

---

## 2. Amazon Linux 2023 部署步骤

### 2.1 实例与安全组

| 项 | 值 |
|---|---|
| 实例类型 | `t4g.small`（Graviton 更便宜）。依赖全是纯 Python（Flask / openpyxl / boto3），ARM 不需要编译 |
| 安全组入站 | 22 ← 只开自己的出口 IP；80、443 ← `0.0.0.0/0`。**不要长期开 5000** |
| 弹性 IP | 必须绑。不绑的话实例 stop/start 后公网 IP 会变，域名解析就断了 |
| IAM 角色 | **不需要**。AWS 凭证来自 `cred.xlsx`，`boto3.client()` 是显式传 AK/SK 的 |

### 2.2 系统准备

```bash
sudo dnf update -y && sudo dnf install -y git nginx
```

### 2.3 Python 3.11 —— 最容易踩的坑

`pyproject.toml` 写的是 `requires-python = ">=3.10"`，而 AL2023 的系统 `python3` 是 3.9。
先确认仓库里有什么：

```bash
dnf list available 'python3.1*' | grep -v devel
```

```bash
sudo dnf install -y python3.11 python3.11-pip
```

建 venv 时**显式用 3.11**，不要用 `python3`：

```bash
cd /opt/bedrock && sudo -u bedrock python3.11 -m venv .venv
```

后面 `.venv/bin/pip install -e ".[deploy]"` 和 DEPLOY.md 6.1 一样。

### 2.4 用户与代码

```bash
sudo useradd --system --no-create-home --home-dir /opt/bedrock --shell /sbin/nologin bedrock
```

RHEL 系的 `useradd` 默认会建同名组，所以 systemd 单元里的 `Group=bedrock` 不用改。

代码和台账照抄 DEPLOY.md 第 5 章（`scp` 的目标用户换成 `ec2-user`，落地后再
`chown bedrock:bedrock`）。

### 2.5 时区 —— 会直接改变页面上的数字

EC2 默认是 **UTC**，而应用的「今天 / 本月」取服务器本地时间（`views.py` 里的
`date.today()`），页面右上角的时间和配额重置窗口也用本地时区。不设的话日期口径会和
UTC+8 的机器差 8 小时：

```bash
sudo timedatectl set-timezone Asia/Kuala_Lumpur
```

改完重启服务。是否生效看 `journalctl` 里 gunicorn 那几行时间戳是不是 `+0800`。

### 2.6 `.env`

照抄 DEPLOY.md 6.2 的生产模板，再用第 0 节的场景表核对那三个开关。

### 2.7 systemd

DEPLOY.md 第 7 章的单元文件在 AL2023 上**可以整段照抄**，包括 `ProtectSystem=strict`
那组加固项。单 worker + 多线程的理由见该章，别改成 `--workers 4`（缓存和登录失败计数
都是进程内状态）。

启动后确认三件事：

```bash
journalctl -u bedrock -n 20 --no-pager
```

- `Listening at: http://127.0.0.1:5000` —— 地址对不对
- `Using worker: gthread` 且只有**一个** `Booting worker with pid` —— 单 worker 多线程生效
- systemd 打出 `Started` 而不是超时 —— `Type=notify` 握手成功

### 2.8 nginx

配置文件内容见第 3 节。启用方式比 Ubuntu 简单，不用软链：

```bash
sudo nginx -t && sudo systemctl enable --now nginx
```

certbot 和 fail2ban 见问题 8、问题 9。

---

## 3. 完整的 nginx 配置

DEPLOY.md 把它拆在 8.1 和 8.3 两节（certbot 会在中间改一次文件），所以那里没有完整版。

### 3.1 裸 IP、无 HTTPS（临时测试用）

`/etc/nginx/conf.d/bedrock.conf`：

```nginx
limit_req_zone $binary_remote_addr zone=bedrock_login:10m rate=10r/m;

server {
    listen 80;
    server_name _;

    server_tokens off;

    # 这四个头在 server 级设一次，所有 location 自动继承
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-For   $remote_addr;   # 覆写，不是追加
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Real-IP         $remote_addr;

    # 冷缓存要查 CloudWatch 四个区 × 多个账号，几十秒是正常的。
    # nginx 默认 proxy_read_timeout 只有 60s，不改会撞 504。
    proxy_connect_timeout 10s;
    proxy_send_timeout   180s;
    proxy_read_timeout   180s;

    location = /login {
        limit_req zone=bedrock_login burst=5 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:5000;
    }

    location / {
        proxy_pass http://127.0.0.1:5000;
    }
}
```

配套：`.env` 里 `HOST=127.0.0.1`、`TRUST_PROXY=true`、`SESSION_COOKIE_SECURE=false`。
安全组开 80、撤掉 5000。

### 3.2 域名 + HTTPS（最终版）

顺序是**先用 3.1 那份把 80 跑通**（certbot 要靠它验证域名），把 `server_name _` 换成域名，
再跑 `certbot --nginx -d <DOMAIN> --redirect`，然后把整个文件替换成下面这份：

```nginx
limit_req_zone $binary_remote_addr zone=bedrock_login:10m rate=10r/m;

server {
    listen 80;
    server_name <DOMAIN>;

    # 放在跳转之前，证书续期走 webroot 时才不会被 301 掉
    location ^~ /.well-known/acme-challenge/ {
        root /usr/share/nginx/html;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl;
    # nginx >= 1.25.1 才有这个指令；1.24 上要删掉它，改成 listen 443 ssl http2;
    http2 on;
    server_name <DOMAIN>;

    ssl_certificate     /etc/letsencrypt/live/<DOMAIN>/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/<DOMAIN>/privkey.pem;
    include             /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam         /etc/letsencrypt/ssl-dhparams.pem;

    server_tokens off;

    # 只补 HSTS。nosniff / X-Frame-Options / Referrer-Policy / Cache-Control
    # 应用自己已经在发了（见 __init__.py 的 _security_headers），
    # nginx 再 add_header 一遍会变成两份重复的响应头。
    add_header Strict-Transport-Security "max-age=31536000" always;

    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-For   $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Real-IP         $remote_addr;

    proxy_connect_timeout 10s;
    proxy_send_timeout   180s;
    proxy_read_timeout   180s;

    location = /login {
        limit_req zone=bedrock_login burst=5 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:5000;
    }

    location / {
        proxy_pass http://127.0.0.1:5000;
    }
}
```

上了 HTTPS 之后把 `.env` 的 `SESSION_COOKIE_SECURE` 改回 `true`，重启后端。

三处和 DEPLOY.md 8.x 不同的地方，都是有意的：

1. **加了 `proxy_read_timeout 180s`**。DEPLOY.md 给 gunicorn 配了 `--timeout 120`，却没动
   nginx 那侧的默认 60 秒 —— 冷缓存查 CloudWatch 时会先在 nginx 这里 504。让 nginx 这个值
   ≥ gunicorn 的 `--timeout`。
2. **`proxy_set_header` 提到了 server 级**，不在两个 location 里各写一遍。注意 nginx 的继承
   规则：只要 location 内部**一条** `proxy_set_header` 都不写，就完整继承 server 级的四条；
   一旦在 location 里写了任意一条，父级的全部失效 —— 所以别往 location 里加。
3. **去掉了重复的三个安全头**。`add_header` 是追加语义，而应用侧用的是 `setdefault`，两边
   都发会让客户端收到两份 `X-Frame-Options: DENY`。HSTS 应用不发，必须由 nginx 补。

> 想让裸 IP 直连的扫描流量拿不到登录页（回 444）需要 `default_server` 块，但 AL2023 的
> `nginx.conf` 里已经有一个 `listen 80 default_server`，直接加会报
> `duplicate default server`，得先把 `nginx.conf` 里那个删掉。

---

## 4. 问题记录

### 问题 1：改了 `.env` 的 `HOST=0.0.0.0`，外网还是访问不到

**现象**：`.env` 已改、服务也重启了，`http://<公网IP>:5000` 打不开。
`journalctl -u bedrock` 里写的仍然是 `Listening at: http://127.0.0.1:5000`。

**原因**：跑的是 gunicorn，监听地址来自 systemd 单元的 `--bind`，与 `.env` 的 `HOST` 无关。
见第 0 节。

**处理**：

```bash
sudo sed -i 's|--bind 127.0.0.1:5000|--bind 0.0.0.0:5000|' /etc/systemd/system/bedrock.service
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart bedrock
```

验证（应该看到 `0.0.0.0:5000`）：

```bash
ss -lntp | grep 5000
```

### 问题 2：端口确实在听，外网还是不通

按这个顺序查，别跳步：

1. **连的是公网 IP 吗** —— `ip-172-31-x-x` 这种主机名里的是私网地址，不能用来访问。
2. **`ss -lntp` 显示的是 `0.0.0.0:5000` 还是 `127.0.0.1:5000`** —— 后者见问题 1。
3. **安全组** —— 规则挂在这台实例上吗？源 IP 是不是写的旧地址？
4. **本地网络出站是否封了非标准端口** —— 很常见，很多公司网络和部分 ISP 只放行 80/443。
   判断方法：在服务器上 `curl http://<公网IP>:5000/login` 通，但自己机器上不通，基本就是它。
   解法是走 nginx + 80 端口，而不是继续折腾 5000。

### 问题 3：能打开登录页，密码是对的，但一直弹回登录页

**原因**：`.env` 里 `SESSION_COOKIE_SECURE=true`（DEPLOY.md 6.2 的生产模板默认开着），
浏览器在纯 http 下不会保存会话 cookie，于是每个请求都是未登录状态。

**处理**：临时测试期间改成 `false`，配好 HTTPS 后改回 `true`。见第 0 节的场景表。

> 这个坑很费时间，因为表现看起来像「密码哈希写错了」。判断方法：浏览器开发者工具
> Application → Cookies 里没有 session 这一项，就是它。

### 问题 4：`pip install -e .` 报 `requires a different Python`

**现象**：`Package 'bedrock-cost' requires a different Python: 3.9.x not in '>=3.10'`。

**原因**：AL2023 的系统 `python3` 是 3.9，而 `pyproject.toml` 要求 `>=3.10`。

**处理**：见 2.3，装 `python3.11` 并用它建 venv。venv 建错了直接 `rm -rf .venv` 重建，
不要试图升级现有 venv。

### 问题 5：nginx 起不来，`bind() to 0.0.0.0:80 failed (98: Address already in use)`

**现象**：`systemctl status nginx` 是 `failed`，但 `ExecStartPre=/usr/sbin/nginx -t` 那行是
`status=0/SUCCESS` —— **配置文件没问题，纯粹是端口被占**。errno 98 就是 `EADDRINUSE`。

**原因**：最常见的是有一个**游离的 nginx master 进程**还活着。AL2023 的 nginx 单元里有
`ExecStartPre=/usr/bin/rm -f /run/nginx.pid`：一旦 pid 文件被删掉或变旧，
`systemctl stop/restart` 就找不到那个 master 去发信号，老进程继续占着 :80，
新的 `ExecStart` 必然撞 EADDRINUSE。

**处理**：先看是谁占着。

```bash
sudo ss -lntp | grep -w 80
```

```bash
pgrep -af nginx
```

如果占用者是 nginx：

```bash
sudo pkill -x nginx
```

```bash
sudo ss -lntp | grep -w 80
```

**上面这条必须没有任何输出**，才往下走：

```bash
sudo systemctl start nginx && systemctl status nginx --no-pager
```

如果占用者不是 nginx：`httpd`（Apache）→ `sudo systemctl disable --now httpd`；
容器 → `sudo docker ps` / `sudo podman ps`，找发布了 `0.0.0.0:80->` 的那个停掉。

起来之后验反代：

```bash
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1/login
```

`200` 正常；`502` 说明 nginx 活着但后端不在 `127.0.0.1:5000`（后端挂了，或 `HOST` /
`--bind` 没改回 `127.0.0.1`）。

### 问题 6：配置写错了却没有任何提示

**原因**：`startup_warnings()`（台账找不到、`SECRET_KEY` 未设、`AUTH_PASSWORD` 为空）只在
`__main__.py` 里打印，**走 gunicorn 的 `create_app()` 路径不会输出**。表现是服务正常启动、
页面能打开，但账号列表是空的。

**处理**：装完之后手动跑一次 DEPLOY.md 6.3 的冒烟测试，确认台账 `True`、提醒「无」、
`secure cookie` 符合预期。走 `python -m bedrock_cost` 时不需要，它会自己打印。

### 问题 7：冷缓存时页面 504

**原因**：查 CloudWatch 四个区 × 多个账号，冷缓存要几十秒。gunicorn 那侧给了
`--timeout 120`，但 nginx 的 `proxy_read_timeout` 默认只有 60 秒，先在 nginx 这里断了。

**处理**：nginx 配置里显式写 `proxy_read_timeout 180s;`（见 3.1），并保证它
≥ gunicorn 的 `--timeout`。

### 问题 8：AL2023 上装不了 certbot

先确认：

```bash
dnf list available certbot python3-certbot-nginx
```

有就照抄 DEPLOY.md 8.2。没有的话用 pip 装：

```bash
sudo python3.11 -m venv /opt/certbot && sudo /opt/certbot/bin/pip install -q certbot certbot-nginx && sudo ln -sf /opt/certbot/bin/certbot /usr/bin/certbot
```

**注意 pip 装的 certbot 不带自动续期的 systemd timer**，要自己加 cron：

```bash
echo "0 3 * * * root /usr/bin/certbot renew --quiet --deploy-hook 'systemctl reload nginx'" | sudo tee /etc/cron.d/certbot-renew
```

### 问题 9：AL2023 上没有 fail2ban

AL2023 默认仓库没有 fail2ban，且**不支持 EPEL**。不值得为它折腾 pip 安装（要配 systemd
后端和一堆路径）。

**处理**：跳过 DEPLOY.md 8.4，把前两道防线收紧就够了 —— `.env` 里
`MAX_LOGIN_ATTEMPTS=5` / `LOCKOUT_SECONDS=900`（应用层按 IP 锁定），加上 nginx 的
`limit_req` 10r/m。真要第三层，用安全组把 22 和 443 的源 IP 收窄，比 fail2ban 有效得多。

### 问题 10：SELinux（预防性）

AL2023 默认是 `permissive`，所以 nginx 反代到 `127.0.0.1:5000` 不会被拦。如果以后调成
`enforcing`，登录页会变 502，需要补：

```bash
sudo setsebool -P httpd_can_network_connect 1
```

### 问题 11：`/opt/bedrock` 里多出一个 `.gunicorn/`

gunicorn 26 会把控制套接字建在工作目录下（`/opt/bedrock/.gunicorn/gunicorn.ctl`）。
不影响运行（`ProtectSystem=strict` 配 `ReadWritePaths=/opt/bedrock` 正好允许写），但
DEPLOY.md 第 9 章「更新代码」是在这个目录里 `git pull`，之后 `git status` 会一直挂着一条
untracked。要清爽就在 `.gitignore` 里加一行 `.gunicorn/`。

### 问题 12：`systemctl status` 的日志被截断成 `Add>`

`systemctl status` 按终端宽度截断。看完整的用：

```bash
journalctl -u nginx -n 30 --no-pager
```

---

## 5. 临时测试与恢复

裸 IP 临时测试（无域名无 HTTPS）会同时放松三样东西：端口暴露、cookie 不要求 HTTPS、
后端绑 `0.0.0.0`。**测完一定要收回来** —— 否则等于长期把登录口令和会话 cookie 明文挂在
公网上，而登录后的页面展示的是真实账号的成本数据。

恢复清单：

- [ ] systemd 单元的 `--bind` 改回 `127.0.0.1:5000`（或 `.env` 的 `HOST` 改回 `127.0.0.1`）
- [ ] `.env` 的 `SESSION_COOKIE_SECURE` 改回 `true`
- [ ] `.env` 的 `TRUST_PROXY`：有 nginx 就 `true`，没有就 `false`
- [ ] `DEBUG=false`（任何时候）
- [ ] 安全组撤掉 5000 入站
- [ ] `sudo systemctl restart bedrock`

---

## 6. 附：从 systemd 切回 `python -m bedrock_cost`

只做临时验证时可以不要 systemd。清理：

```bash
sudo systemctl disable --now bedrock
```

```bash
sudo rm -f /etc/systemd/system/bedrock.service && sudo rm -rf /etc/systemd/system/bedrock.service.d
```

```bash
sudo systemctl daemon-reload && sudo systemctl reset-failed
```

```bash
sudo rm -rf /opt/bedrock/.gunicorn
```

确认干净（两条都应该没有输出）：

```bash
pgrep -af gunicorn; ss -lntp | grep 5000
```

启动（这时候 `.env` 的 `HOST` 才真正生效）：

```bash
cd /opt/bedrock && sudo -u bedrock .venv/bin/python -m bedrock_cost
```

这条路径的两个差别：

1. **前台运行，SSH 一断就停**。要它活着挂 tmux：`sudo dnf install -y tmux`，
   在 `tmux new -s bedrock` 里跑，`Ctrl-B D` 脱离。
2. **不再开机自启**，实例重启要手动拉起来。

好处是 `startup_warnings()` 会打印出来（见问题 6），配置错误不会静默。
