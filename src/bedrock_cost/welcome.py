"""概览顶上的欢迎条：「欢迎回来，名字」、今天在线多久，旁边一条小小的像素风景（云、远山、树、草），
几只 Claude 像素小家伙在上面走来走去、跳舞。

鼠标移到哪只身上，它就停下来举手、冒个对话框说一句；点一下蹦一下、换一句（被戳了有专门的反应，POKED）。
说什么（static/mascots.js 挑）：
  · 每只有自己的脾气（MOODS）：爱吐槽账单的、卖萌的、讲冷笑话的、监工的，一多半说自己那一类；
  · 大家都会说的（SHARED）；
  · 看场合说的（context_lines）：叫你的名字，看现在几点、星期几、今天在线多久，看概览上的数
    （几个账号在跑、几个在发呆、几个打着风控、额度用了多少、谁最能花、几个客户）。
走、停、跳舞是脚本随机安排的；风景跟着它们动：远处的云和山、树按它们站的地方慢慢挪（视差），
走过去的地方草被踩弯、树叶晃一晃掉一片，走路时脚后面扬一点小灰。系统设了「减少动态效果」就都站着不动，
风景也不动，对话框照样有。

小家伙就是侧边栏、登录页上那只（tools/make_mascot_assets.py 的格子，10 × 8）；帧和风景都在 templates/_mascot.html。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import customers, presence, users

WEEKDAYS = "一二三四五六日"

SHARED = (
    "人类怎么了？",
    "人类快去工作！",
    "又来看账单啦？",
    "我只是路过，什么都没看见。",
    "一起跳个舞吧！",
    "今天也要加油哦。",
    "你看起来需要一杯咖啡。",
    "嗨！我在这儿！",
    "别盯着我，看数字。",
    "我是这一页最可爱的东西。",
    "小心，别踩到草。",
    "嘘，别吵醒那朵云。",
    "那棵树是我的好朋友。",
    "我刚数了云，有三朵。",
    "今天的风有点像素化。",
    "你点鼠标的样子真专业。",
    "听说你在找 bug？不是我干的。",
    "我四条腿走路，比你快。",
)

MOODS = {
    # 爱吐槽账单的
    "bill": (
        "今天的 token 也烧得很旺。",
        "Cost Explorer 说它有点累。",
        "额度还够，大概吧。",
        "嘘——我在偷听 CloudWatch。",
        "风控？什么风控？我什么都不知道。",
        "钱不会自己长出来的。",
        "每一个 token 都是血汗钱。",
        "账单晚两天才出，别慌。",
        "建议也给我开个额度。",
        "零太多了，我数不过来。",
        "上量了！快看！……哦，看错了。",
        "AWS 没发邮件，就是好消息。",
        "我把余额藏在树洞里了。",
        "这个月的账单，我不敢看。",
    ),
    # 卖萌的
    "cute": (
        "摸摸头？",
        "我饿了，有像素吃吗？",
        "抱抱！……算了，我没有手。",
        "我今天洗过澡了，闪闪发亮。",
        "你笑起来比余额还好看。",
        "我可以在这里待一整天。",
        "别走嘛，再陪我一会儿。",
        "我是像素做的，永远不会胖。",
        "你是来看我的吧？我就知道。",
        "我的眼睛只有一个像素，但看得很清楚。",
        "今天的我，是不是有点帅？",
        "跳舞是我的特长，吃饭也是。",
    ),
    # 讲冷笑话、说大道理的
    "deep": (
        "如果账单没人看，它还算钱吗？",
        "宇宙的答案是 42，余额不是。",
        "我思故我在，我跳故我快乐。",
        "云为什么不交电费？",
        "一个像素，也有它的梦想。",
        "人生就像折线图，有起有落。",
        "你知道吗？我其实是一张 SVG。",
        "我在想，树会不会也在看我。",
        "走路的意义，是为了停下来跳舞。",
        "有些 bug，是生命的一部分。",
        "山那边，是不是也有个控制台？",
        "token 去哪儿了？去它该去的地方。",
    ),
    # 监工
    "boss": (
        "摸鱼被我抓到了！",
        "再看一眼就去工作，好吗？",
        "Claude 派我来监督你。",
        "这个页面你今天看过了吧？",
        "数字不会自己变好看的。",
        "我数到三，你就去干活。一、二……",
        "别躲了，我看见你了。",
        "你的待办事项在哭泣。",
        "今天的目标完成了吗？",
        "我在记小本本。",
        "工作使我快乐。骗你的。",
        "老板来了！……开玩笑的。",
    ),
}

# 被点了：一多半说这些
POKED = (
    "哎哟！",
    "别戳了，好痒！",
    "再戳我就跳舞了！",
    "好啦好啦，我听着呢。",
    "你力气好大！",
    "戳我也不会掉金币的。",
    "嘿！我在工作呢！",
    "被发现了……",
    "我要告诉 Claude！",
    "痒痒痒——",
    "再戳一下试试？",
    "我蹦！",
)


@dataclass(frozen=True)
class Mascot:
    scale: int      # 一格画多少像素：10 × 8 的格子画成 10s × 8s，整数倍才不糊
    blink: float    # 眨眼错开多少秒，几只不一起眨
    start: float    # 一开始站在台子的哪儿（0 最左，1 最右），脚本接着随机走
    mood: str       # 脾气，MOODS 的 key：说话一多半挑这一类


MASCOTS = (Mascot(4, 0.0, 0.08, "bill"), Mascot(3, 1.3, 0.34, "cute"), Mascot(5, 2.1, 0.6, "deep"),
           Mascot(4, 3.4, 0.86, "boss"))


@dataclass(frozen=True)
class Facts:
    """概览上的几个数，小家伙拿来说事。查不到的就空着，那几句不说。"""
    accounts: int = 0
    active: int = 0           # 用量状态是「活跃」的
    quiet: int = 0            # 「已中断」「无调用」的
    risky: int = 0            # 生命周期打着「风控」的
    usage_pct: float | None = None
    balance: float | None = None
    top: str = ""             # 累计花得最多的账号（邮箱 @ 前面那段，没邮箱用号码）
    customers: int = 0


def facts_from(cards, report, customer_count: int) -> Facts:
    """从概览的卡片和合计里拿那几个数。cards 是 dashboard.CardRow，report 是 report.Report。"""
    states = [card.state for card in cards]
    spenders = sorted((card for card in cards if card.has_numbers), key=lambda card: -card.total_cost)
    top = spenders[0] if spenders and spenders[0].total_cost > 0 else None
    return Facts(
        accounts=len(cards),
        active=states.count("active"),
        quiet=states.count("stopped") + states.count("idle"),
        risky=sum(1 for card in cards if "风控" in card.lifecycle),
        usage_pct=report.total_usage_pct if report.total_budget > 0 else None,
        balance=report.total_balance if report.total_budget > 0 else None,
        top=((top.email or "").split("@")[0] or top.label) if top else "",
        customers=customer_count,
    )


def context_lines(person: users.Person, seconds: int, now: datetime, facts: Facts | None = None) -> list[str]:
    """看场合说的：叫名字，看几点、星期几、今天在线多久，看概览上的数。"""
    out = [f"{person.label}，欢迎回来！", f"{person.label}，今天也很努力呢。"]
    hour = now.hour
    if hour < 6:
        out.append("这么晚了还不睡？人类真奇怪。")
    elif hour < 11:
        out.append("早上好！咖啡喝了吗？")
    elif hour < 14:
        out.append("该吃午饭了，账单又不会跑。")
    elif hour < 18:
        out.append("下午三点，最适合摸鱼……不是，工作。")
    elif hour < 22:
        out.append("晚上好，今天辛苦啦。")
    else:
        out.append("夜深了，Cost Explorer 都睡了。")
    weekday = now.weekday()
    if weekday == 0:
        out.append("周一……人类也觉得很难吧？")
    elif weekday == 4:
        out.append("周五啦！周末想好去哪了吗？")
    elif weekday >= 5:
        out.append(f"周{WEEKDAYS[weekday]}还来看账单，你是真的爱。")
    if seconds >= 2 * 3600:
        out.append(f"今天已经在线 {presence.label(seconds)} 了，起来走两步吧。")
    elif seconds < 120:
        out.append("刚来呀？先喝口水。")
    if facts:
        if facts.active:
            out.append(f"今天有 {facts.active} 个账号在干活。")
        if facts.quiet:
            out.append(f"{facts.quiet} 个账号在发呆，要不要去看看？")
        if facts.risky:
            out.append(f"{facts.risky} 个账号打着风控……不是我干的。")
        if facts.usage_pct is not None:
            out.append(f"额度已经用掉 {facts.usage_pct:.0f}% 了。")
        if facts.balance is not None and facts.balance > 0:
            out.append(f"余额还有 {customers.short_money(facts.balance)}，稳住。")
        if facts.top:
            out.append(f"{facts.top} 最能花，我看见了。")
        if facts.customers:
            out.append(f"{facts.customers} 个客户在用，人类真受欢迎。")
    return out


def context(person: users.Person, now: datetime | None = None, facts: Facts | None = None) -> dict:
    """概览模板要的：谁、今天在线多少秒（和说人话的写法）、今天几号星期几、白天还是夜里（画太阳还是月亮），
    小家伙们和它们能说的话。"""
    now = now or datetime.now()
    seconds = presence.seconds_on(person.name, now.date())
    return {
        "person": person,
        "seconds": seconds,
        "online": presence.label(seconds),
        "today": f"{now.month} 月 {now.day} 日 星期{WEEKDAYS[now.weekday()]}",
        "night": now.hour < 6 or now.hour >= 19,
        "mascots": MASCOTS,
        "talk": {"shared": list(SHARED), "moods": {mood: list(lines) for mood, lines in MOODS.items()},
                 "poked": list(POKED), "context": context_lines(person, seconds, now, facts)},
    }
