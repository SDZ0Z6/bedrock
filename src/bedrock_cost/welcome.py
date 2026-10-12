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

背景（SCENES）：白天、黄昏、夜晚、夏夜萤火虫、下雨、雨后彩虹、下雪、樱花、秋天落叶。默认按时间和季节自动挑
（auto_scene），欢迎条右上角能自己挑，挑过的记在自己的资料里（users.Look.scene，空着是自动）。换背景不用刷新页面：
颜色由 style.css 按 data-kind 换，雨、雪、花瓣、落叶、萤火虫由 mascots.js 撒。小家伙也跟着变：
  · 台词（SCENE_LINES）：下雨说「记得带伞」、下雪说「账单都冻住了」……；
  · 小道具：下雨撑伞，下雪戴围巾，樱花、秋天头上顶一片花瓣 / 叶子；
  · 动作：夜里有一只睡着了冒「Zzz」（说梦话，SLEEPY），下雪走着走着会打滑，下雨爱往树底下躲。

小家伙就是侧边栏、登录页上那只（tools/make_mascot_assets.py 的格子，10 × 8）；帧和风景都在 templates/_mascot.html。
"""

from __future__ import annotations

import math
import zlib
from dataclasses import dataclass
from datetime import date, datetime

from . import customers, presence, users

WEEKDAYS = "一二三四五六日"

# 背景：key -> 名字。顺序就是挑背景那个菜单里的顺序
SCENES = {
    "day": "白天",
    "dusk": "黄昏",
    "night": "夜晚",
    "fireflies": "夏夜萤火虫",
    "rain": "下雨",
    "rainbow": "雨后彩虹",
    "snow": "下雪",
    "sakura": "樱花",
    "autumn": "秋天落叶",
}

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

# 跟着背景说的
SCENE_LINES = {
    "day": ("今天天气真好。", "晒太阳中，勿扰。", "阳光正好，适合……工作。", "云在天上，账单在页面上。", "这么好的天，token 也想出去玩。"),
    "dusk": ("太阳要下班了，你呢？", "晚霞好看吧？比账单好看。", "天边红红的，像不像使用率？", "黄昏了，今天的 token 烧完了吗？",
             "落日的余晖，照在余额上。"),
    "night": ("好困……", "夜深了，账单也要睡觉。", "嘘，星星在数数。", "月亮今天也在加班。", "熬夜对像素不好。",
              "我们小声点，别吵醒它。"),
    "fireflies": ("萤火虫在开派对！", "一闪一闪，像告警灯。", "夏天的晚上最舒服了。", "我想抓一只萤火虫……可惜没有手。",
                  "它们是不是也在烧 token？"),
    "rain": ("下雨了，记得带伞！", "我的伞是像素做的，有点漏。", "雨声好适合写代码。", "云在哭，可能看到账单了。",
             "别淋湿了，会短路的。", "快到树底下躲雨！"),
    "rainbow": ("看！彩虹！", "雨停了，彩虹出来了。", "彩虹的尽头有额度吗？", "踩水坑最好玩了！", "七种颜色，比折线图好看。"),
    "snow": ("好冷，账单都冻住了。", "下雪啦！堆个雪人吧！", "围巾是 Claude 织的。", "小心，地上滑！", "雪花和 token 一样多。",
             "哎呀，又滑了一跤。"),
    "sakura": ("花瓣掉我头上了。", "樱花开了，春天来了。", "好香……是花还是咖啡？", "樱花雨！快许愿！", "春天适合上量。"),
    "autumn": ("秋天到了，叶子在下班。", "落叶好好踩，咔嚓咔嚓。", "秋高气爽，适合对账。", "一片叶子掉我头上了。",
               "秋天的风有点凉。"),
}

# 夜里睡着的那只说梦话
SLEEPY = ("呼……呼……", "再睡五分钟……", "Zzz……额度……还够……", "别吵，梦到上量了。", "呼噜……", "唔……账单……明天再看……")

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


def rainy(day: date) -> bool:
    """哪天下雨：按日期算的，大约一周一天；同一天谁来看都一样。"""
    return zlib.crc32(day.isoformat().encode("ascii")) % 7 == 0


def auto_scene(now: datetime) -> str:
    """自动挑背景：晚上 7 点到早上 6 点是夜里（6–8 月有萤火虫），下午 5 点到 7 点是黄昏；白天看月份——12–2 月下雪，
    下雨的日子（rainy）上午下雨、下午 2 点以后雨停了出彩虹，3–4 月樱花，9–11 月秋天落叶，别的日子是晴天。"""
    hour, month = now.hour, now.month
    if hour < 6 or hour >= 19:
        return "fireflies" if month in (6, 7, 8) else "night"
    if hour >= 17:
        return "dusk"
    if month in (12, 1, 2):
        return "snow"
    if rainy(now.date()):
        return "rainbow" if hour >= 14 else "rain"
    if month in (3, 4):
        return "sakura"
    if month in (9, 10, 11):
        return "autumn"
    return "day"


def _rainbow(radius: int = 18, bands: int = 6) -> list[tuple[int, int, int, int]]:
    """彩虹：半个圆环，一圈一个颜色，按格子画（一行里同一圈连着的格子并成一个矩形）。
    返回 [(x, y, 宽, 第几圈)]，画在 2·radius × radius 的格子里，圆心在底边正中。"""
    out = []
    for y in range(radius):
        run = None   # [起点, 宽, 圈]
        for x in range(2 * radius + 1):
            band = None
            if x < 2 * radius:
                gap = radius - math.hypot(x + 0.5 - radius, y + 0.5 - radius)
                band = int(gap) if 0 <= gap < bands else None
            if run and band == run[2]:
                run[1] += 1
                continue
            if run:
                out.append((run[0], y, run[1], run[2]))
            run = [x, 1, band] if band is not None else None
    return out


RAINBOW = _rainbow()


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
    """概览模板要的：谁、今天在线多少秒（和说人话的写法）、今天几号星期几，背景（这个人挑的，没挑就是自动挑的），
    小家伙们和它们能说的话。"""
    now = now or datetime.now()
    seconds = presence.seconds_on(person.name, now.date())
    chosen = person.look.scene if person.look.scene in SCENES else ""
    auto = auto_scene(now)
    return {
        "person": person,
        "seconds": seconds,
        "online": presence.label(seconds),
        "today": f"{now.month} 月 {now.day} 日 星期{WEEKDAYS[now.weekday()]}",
        "scene": {"kind": chosen or auto, "auto": auto, "chosen": chosen, "options": list(SCENES.items()),
                  "rainbow": RAINBOW},
        "mascots": MASCOTS,
        "talk": {"shared": list(SHARED), "moods": {mood: list(lines) for mood, lines in MOODS.items()},
                 "poked": list(POKED), "context": context_lines(person, seconds, now, facts),
                 "scenes": {kind: list(lines) for kind, lines in SCENE_LINES.items()}, "sleepy": list(SLEEPY)},
    }
