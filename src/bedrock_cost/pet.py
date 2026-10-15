"""每个页面底下走来走去的那只像素小家伙（登录后的页面都有，登录页没有）：概览欢迎条上那几只的同类，同一套格子
（templates/_mascot.html 的 pet()），怎么动在 static/pet.js。

它沿着屏幕边爬：大多数时候在底下走来走去、停一会儿、跳个舞；隔一阵走到边上爬上墙（宽屏上左边的墙是侧边栏的边），
爬到顶上倒挂着走，再从另一边爬下来，或者一松手掉下来。和人互动：
  · 鼠标放上去：停下来举手，冒个对话框说一句——看是哪一页说哪一页的事（PAGE_LINES），在墙上、顶上有专门的
    （CLIMB、CEILING），也叫你的名字、看几点（welcome.time_lines），也说大家都会说的（SHARED、欢迎条那几只的脾气话）；
  · 点一下：蹦一下、换一句，被戳了多半说「哎哟」那一类（welcome.POKED）；
  · 按住拖：拎起来手脚乱蹬（HELD），松手按甩出去的快慢飞出去，摔得重还会弹两下，落地说一句（LANDED）；
    自己从顶上、墙上松手时喊一声（FALL）；
  · 眼睛跟着鼠标：鼠标冲过来太快太近会吓一跳、跑开（DODGE）；
  · 右键：「收起小家伙」（BYE），记在自己的资料里（users.Look.pet，POST /me/pet），哪个页面都不出来了；
    资料页上的开关能再打开。
换页面它接着在原来的地方（sessionStorage，一个标签页一只）；一个标签页里第一次出来是从顶上掉下来的（ARRIVE）。
"""

from __future__ import annotations

import random
from datetime import datetime

from flask import url_for

from . import users, welcome
from .auth import csrf_token

# 看是哪一页（模板里的 active_page）说的
PAGE_LINES = {
    "overview": (
        "上面欢迎条里那几只是我亲戚。",
        "这么多卡片，你先看哪张？",
        "红的黄的绿的，像不像红绿灯？",
        "今天的总数，我帮你盯着呢。",
        "概览看完了？去客户页转转。",
        "我从这儿能看到所有账号。",
    ),
    "customers": (
        "客户越多，我爬得越开心。",
        "这个客户今天上量了吗？",
        "调用次数在涨，我看见了。",
        "预估那几根柱子，像不像我的腿？",
        "哪个客户最能花？我不说。",
        "切「看多久」不用刷新整页，我作证。",
    ),
    "account": (
        "这个账号我熟。",
        "六个页签，一个一个点吧。",
        "时间线上的事，我都记着呢。",
        "配额还够吗？我帮你数数。",
        "这个账号今天辛苦了。",
        "预估是 CloudWatch 算的，账单才算数。",
    ),
    "ops": (
        "毛利！毛利！",
        "收入涨了吗？我想听好消息。",
        "看板上的数，比我还会跳。",
        "需要关注的那一栏，别忘了看。",
        "运营的人最辛苦了。",
    ),
    "accounts": (
        "改台账要小心，别手抖。",
        "删之前先想三秒。",
        "新账号？欢迎欢迎！",
        "额度别多填一个零。",
        "TG 告警开着吗？",
        "这一页只有管理员能来，你好厉害。",
    ),
    "people": (
        "又来新同事了？",
        "密码只显示一次，记得抄下来。",
        "只读用户改不了东西，但能看我跳舞。",
        "停用之前，跟人家说一声吧。",
        "谁在线最久？我不说。",
    ),
    "settings": (
        "操作日志里有我吗？",
        "谁半夜还在登录？",
        "这里很安静，适合打个盹。",
        "记录都在，别担心。",
        "登录失败的那几行，我也盯着呢。",
    ),
    "me": (
        "这是你的资料页，我也想要一个。",
        "换个头像吧，我觉得 🦊 不错。",
        "想收起我？右键点我就行……别吧。",
        "名字改好听点，我好叫你。",
        "你今天在线多久了？上面写着呢。",
    ),
}

# 大家都会说的（哪一页都行）
SHARED = (
    "我住在屏幕边上。",
    "这一页的边边角角我都爬过。",
    "拖我试试？我很轻的。",
    "我会爬墙，你会吗？",
    "顶上的风景不错，下次带你去。",
    "我在巡逻，一切正常。",
    "嗨！我在这儿！",
    "别盯着我，看数字。",
    "我是这一页最可爱的东西。",
    "我四条腿走路，比你快。",
    "你点鼠标的样子真专业。",
    "听说你在找 bug？不是我干的。",
    "我跟着你换页面，你去哪我去哪。",
    "右下角是我的地盘。",
)

CLIMB = ("我在攀岩！", "墙上的风景不一样。", "别催，我在往上爬。", "我是一只壁虎。", "这面墙有点滑……",
         "爬到顶上就能看到全部了。")
CEILING = ("倒过来看，账单也一样。", "血往头上涌……", "我在天花板上！看我！", "倒着看，涨的像跌的。",
           "蝙蝠就是这么看世界的。", "别抬头，我在上面。")
HELD = ("放我下来！", "哇——好高！", "轻点轻点！", "我恐高……", "要带我去哪？", "抓稳了，别松手！")
LANDED = ("安全着陆！", "十分！", "再来一次！", "屁股好疼……", "我没事，我是像素做的。", "地面，我回来了。")
FALL = ("啊——", "我跳啦！", "手滑了！", "接住我！")
DODGE = ("吓我一跳！", "鼠标好快！", "别过来！", "差点被抓住！", "你追不上我！")
ARRIVE = ("我来啦！", "空降成功！", "嘿咻——落地！", "从天而降的小家伙！")
BYE = ("拜拜！想我了去「我的资料」叫我。", "我先躲起来了，想我了去「我的资料」。")

MOOD_SAMPLE = 12   # 欢迎条那几只的脾气话（welcome.MOODS）每页随机带几句：全带上每个页面要多几 KB


def lines(person: users.Person, page: str, now: datetime | None = None) -> dict:
    """这一页小家伙能说的话（pet.js 按情形挑）。"""
    moods = [line for pool in welcome.MOODS.values() for line in pool]
    return {
        "page": list(PAGE_LINES.get(page, ())),
        "context": welcome.time_lines(person, now or datetime.now()),
        "shared": list(SHARED),
        "moods": random.sample(moods, min(MOOD_SAMPLE, len(moods))),
        "poked": list(welcome.POKED),
        "climb": list(CLIMB), "ceiling": list(CEILING), "held": list(HELD), "landed": list(LANDED),
        "fall": list(FALL), "dodge": list(DODGE), "arrive": list(ARRIVE), "bye": list(BYE),
    }


def conf(person: users.Person, page: str = "") -> dict:
    """shell.html 里 #pet-conf 那一段：右键「收起小家伙」往哪发（带令牌），这一页能说的话。"""
    return {"url": url_for("profile.pet"), "csrf": csrf_token(), "page": page or "", "lines": lines(person, page or "")}
