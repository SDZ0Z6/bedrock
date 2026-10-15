/* 每个页面底下那只像素小家伙（pet.py 的台词、templates/_mascot.html 的 pet()；shell.html 里登录后的每个页面都有一只）。
 *
 * 整个窗口是它的屋子：底、右墙、顶、左墙四条边（宽屏上左墙是侧边栏的边），它总在其中一条边上，位置是「哪条边 + 边上的
 * 哪儿」。一圈逆时针是 底 → 右墙 → 顶 → 左墙 → 底，dir = 1 就是往这个方向走（在底下就是往右走）：走到边的尽头转过墙角
 * （身子跟着转 90 度）上了下一条边，dir 不变。在墙上脚贴着墙、头朝屋子里，在顶上倒挂着。
 *
 * 平时干嘛（随机安排）：大多在底下走来走去、停一会儿、跳个舞；隔一阵（CLIMB_EVERY）走到边上爬上墙，扒着歇一会儿，
 * 再爬到顶上倒挂着走一段，然后从另一边爬下来，或者晃两下一松手掉下来（翻个身，脚先着地）。
 *
 * 和人互动：
 *   · 鼠标放上去（或者用键盘移到它身上）：停下来举手，冒个对话框说一句——看是哪一页说哪一页的事，在墙上、顶上有专门的；
 *   · 点一下：蹦一下、换一句（被戳了多半是「哎哟」那一类）；手机上点一下也停下来说一句，说完接着走；
 *   · 按住拖：拎起来举着手、腿乱蹬，跟着指针晃（往右拖身子往左甩）；松手按甩出去的快慢飞出去，碰到墙弹回来，
 *     摔得重会在地上弹两下，落地说一句；
 *   · 眼睛跟着鼠标：鼠标在附近，两只眼睛往那边看（各挪一格）；在底下时鼠标冲过来又快又近，有时候吓一跳、跑开；
 *   · 右键（手机上长按）：「收起小家伙」，记在自己的资料里（POST /me/pet），哪个页面都不出来了；资料页上能再打开。
 *
 * 换页面它接着在原来的地方，正往哪儿走也接着走（sessionStorage，一个标签页一只）；一个标签页里第一次出来是从顶上掉下来的。
 * 系统设了「减少动态效果」：站在底下不走不爬，说话、拖动照样有（松手直接落在底下）。页面放到后台时 rAF 自己会停。 */
(function () {
  'use strict';
  const layer = document.querySelector('[data-pet-layer]');
  const source = document.getElementById('pet-conf');
  if (!layer || !source) return;
  const conf = JSON.parse(source.textContent);
  const talk = conf.lines || {};
  const pet = layer.querySelector('[data-pet]');
  const bubble = layer.querySelector('[data-pet-bubble]');
  const menu = layer.querySelector('[data-pet-menu]');
  if (!pet || !bubble || !menu) return;
  const still = window.matchMedia('(prefers-reduced-motion: reduce)');
  const wide = window.matchMedia('(min-width: 901px)');    // 宽屏上侧边栏竖在左边（窄屏是顶上的横条，style.css）
  const sidebar = document.querySelector('.sidebar');

  const KEY = 'bedrock-pet';          // sessionStorage：换页面接着在原来的地方
  const G = 2400;                     // 往下掉的加速度（像素 / 秒²）
  const MAX_FALL = 2600;              // 最快掉多快
  const THROW_MAX = 2800;             // 甩出去最快多快
  const BOUNCE = 0.42;                // 摔到地上弹起来剩几成
  const BOUNCE_MIN = 650;             // 落地比这快才弹
  const WALK = [30, 44];              // 在底下每秒走多少像素（每个页面随机一个）
  const CLIMB = 46, CEIL = 40, RUN = 150;   // 爬墙、倒挂着走、被吓跑
  const REST = [1200, 3200], DANCE = [2200, 4600], CLING = [900, 2600], HANG = [1500, 3400];   // 毫秒
  const CLIMB_EVERY = [60000, 150000];      // 在底下待多久才去爬一次墙
  const FIRST_CLIMB = [15000, 35000];       // 刚出来：早一点去爬，让人看见它会爬
  const TURN_MS = 300;                // 转过一个墙角
  const WOBBLE_MS = 650;              // 松手之前晃两下
  const LOOK_NEAR = 240;              // 鼠标离眼睛多近就看着它
  const DODGE_SPEED = 1500, DODGE_NEAR = 110, DODGE_GAP = 9000;   // 鼠标多快、多近吓它一跳；吓完多久不再吓

  const LOOP = ['floor', 'right', 'ceiling', 'left'];                 // 逆时针一圈，dir = 1 往这个方向走
  const ALONG = { floor: 1, right: -1, ceiling: -1, left: 1 };       // dir = 1 时边上的坐标是变大还是变小
  const BASE = { floor: 0, right: -90, ceiling: -180, left: -270 };  // 在这条边上身子转多少度
  const INWARD = { floor: [0, -1], ceiling: [0, 1], left: [1, 0], right: [-1, 0] };   // 头朝哪：屋子里面
  const rand = function (range) { return range[0] + Math.random() * (range[1] - range[0]); };
  const clamp = function (value, low, high) { return Math.min(Math.max(value, low), high); };

  let W = 40, H = 32;                 // 小家伙多大（没转过的样子），measure() 量
  const room = { left: 0, top: 0, right: 0, bottom: 0 };
  // edge、s：在哪条边上、边上的哪儿（脚底中间，横的边是 x、竖的边是 y）；x、y 是脚底中间在屏幕上的位置；
  // cx、cy 是往下掉的时候身子正中（按它算碰撞，身子绕着它翻过来）
  const st = { edge: 'floor', s: 0, dir: -1, angle: 0, x: 0, y: 0, cx: 0, cy: 0, vx: 0, vy: 0,
               mode: 'rest', until: 0, target: 0, pass: false, pace: 0, plan: '', turn: null,
               climbAt: 0, dodgeAt: 0, thrown: false, arriving: false };
  const speed = rand(WALK);
  let hovered = false;                // 鼠标在它身上（或者键盘停在它身上、手机上刚点了它）：停下来说话
  let gone = false;                   // 收起来了
  let press = null;                   // 按下去了，还没拖开
  let drag = null;                    // 拖着
  let swallowClick = false;           // 拖完松手那一下不算「点一下」

  // ------------------------------------------------------------ 屋子有多大、它在哪
  function flat(edge) { return edge === 'floor' || edge === 'ceiling'; }
  function lo(edge) { return (flat(edge) ? room.left : room.top) + W / 2; }
  function hi(edge) { return Math.max(lo(edge), (flat(edge) ? room.right : room.bottom) - W / 2); }
  function line(edge) {
    return edge === 'floor' ? room.bottom : edge === 'ceiling' ? room.top : edge === 'left' ? room.left : room.right;
  }
  function toFoot() {
    if (flat(st.edge)) { st.x = st.s; st.y = line(st.edge); } else { st.x = line(st.edge); st.y = st.s; }
  }
  function settled() { return st.mode !== 'fall' && st.mode !== 'drag' && st.mode !== 'turn'; }
  function centre() {                 // 身子正中：脚底中间往头那边半个身子
    const r = st.angle * Math.PI / 180;
    return [st.x + Math.sin(r) * H / 2, st.y - Math.cos(r) * H / 2];
  }
  function measure() {
    W = pet.offsetWidth || W;
    H = pet.offsetHeight || H;
    room.left = wide.matches && sidebar ? Math.max(0, Math.round(sidebar.getBoundingClientRect().right)) : 0;
    room.top = 0;
    room.right = Math.max(room.left + W, layer.clientWidth);
    room.bottom = Math.max(room.top + H, layer.clientHeight);
    if (settled()) {
      st.s = clamp(st.s, lo(st.edge), hi(st.edge));
      st.target = clamp(st.target, lo(st.edge), hi(st.edge));
      toFoot();
    }
  }

  // ------------------------------------------------------------ 帧：哪一套露出来由 style.css 按 class 管
  function sync() {
    const busy = hovered || !menu.hidden;
    pet.classList.toggle('is-walking', st.mode === 'walk' && !busy);
    pet.classList.toggle('is-dancing', st.mode === 'dance' && !busy);
    pet.classList.toggle('is-talking', hovered && settled());
    pet.classList.toggle('is-dragging', st.mode === 'drag');
    pet.classList.toggle('is-falling', st.mode === 'fall');
    pet.classList.toggle('is-slip', st.mode === 'wobble');
    pet.classList.toggle('on-floor', st.edge === 'floor' && settled());
  }
  function setMode(mode, now, range) {
    st.mode = mode;
    if (mode === 'rest') st.until = now + rand(range || REST);
    else if (mode === 'dance') st.until = now + rand(DANCE);
    else if (mode === 'wobble') st.until = now + WOBBLE_MS;
    sync();
  }
  function hop() {
    if (still.matches) return;
    pet.classList.remove('is-hop');
    void pet.offsetWidth;             // 去掉再加上，动画才会重新放一遍
    pet.classList.add('is-hop');
  }
  function squash() {
    if (still.matches) return;
    pet.classList.remove('is-land');
    void pet.offsetWidth;
    pet.classList.add('is-land');
  }
  pet.addEventListener('animationend', function (event) {
    if (event.animationName === 'pet-hop') pet.classList.remove('is-hop');
    if (event.animationName === 'pet-land') pet.classList.remove('is-land');
  });

  // ------------------------------------------------------------ 走：沿着这条边走到 target；pass 是走到头接着转过墙角
  function go(target, pace, pass, now) {
    st.target = clamp(target, lo(st.edge), hi(st.edge));
    st.pass = !!pass;
    st.pace = pace;
    const sign = st.pass ? (st.target <= lo(st.edge) ? -1 : 1) : (st.target >= st.s ? 1 : -1);
    st.dir = sign * ALONG[st.edge];
    setMode('walk', now);
  }
  function walkStep(dt, now) {
    const forward = st.dir * ALONG[st.edge] > 0;
    const next = st.s + st.pace * dt * (forward ? 1 : -1);
    const reached = forward ? next >= st.target : next <= st.target;
    st.s = reached ? st.target : next;
    toFoot();
    if (!reached) return;
    if (st.pass) turn(now); else decide(now);
  }
  // 转过墙角：脚从这条边的头上挪到下一条边的头上，身子转 90 度；中间往屋子里鼓一点，斜着的时候别伸出窗口
  function turn(now) {
    const edge = LOOP[(LOOP.indexOf(st.edge) + st.dir + 4) % 4];
    const s = clamp(line(st.edge), lo(edge), hi(edge));
    st.turn = { from: st.edge, edge: edge, s: s, x0: st.x, y0: st.y, x1: flat(edge) ? s : line(edge),
                y1: flat(edge) ? line(edge) : s, a0: st.angle, a1: st.angle - 90 * st.dir, start: now };
    setMode('turn', now);
  }
  function turnStep(now) {
    const t = still.matches ? 1 : clamp((now - st.turn.start) / TURN_MS, 0, 1);
    const ease = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
    const a = INWARD[st.turn.from], b = INWARD[st.turn.edge];
    const bulge = Math.sin(Math.PI * t) * W * 0.2 * Math.SQRT1_2;
    st.x = st.turn.x0 + (st.turn.x1 - st.turn.x0) * ease + (a[0] + b[0]) * bulge;
    st.y = st.turn.y0 + (st.turn.y1 - st.turn.y0) * ease + (a[1] + b[1]) * bulge;
    st.angle = st.turn.a0 + (st.turn.a1 - st.turn.a0) * ease;
    if (t < 1) return;
    const from = st.turn.from;
    st.edge = st.turn.edge;
    st.s = st.turn.s;
    st.angle = BASE[st.edge];
    st.turn = null;
    toFoot();
    arrive(from, now);
  }

  // ------------------------------------------------------------ 接下来干嘛
  // 刚转过墙角上了一条新边
  function arrive(from, now) {
    const span = hi(st.edge) - lo(st.edge);
    if (st.edge === 'floor') {
      st.plan = '';
      st.climbAt = now + rand(CLIMB_EVERY);
      return go(st.s + st.dir * ALONG.floor * rand([60, 160]), speed, false, now);   // 下来了：接着往前走一段
    }
    if (st.edge === 'ceiling') {
      st.plan = 'hang';                // 倒挂着往前走一段，停下来歇一会儿
      return go(st.s + st.dir * ALONG.ceiling * rand([0.25, 0.7]) * span, CEIL, false, now);
    }
    if (from === 'floor') {
      st.plan = 'cling';               // 从底下爬上来：爬到一半上下，扒着歇一会儿
      return go(lo(st.edge) + rand([0.15, 0.6]) * span, CLIMB, false, now);
    }
    if (Math.random() < 0.7) {         // 从顶上下来：多半一路爬到底，有时候爬一半松手
      st.plan = '';
      return go(hi(st.edge), CLIMB, true, now);
    }
    st.plan = 'drop';
    return go(lo(st.edge) + rand([0.3, 0.7]) * span, CLIMB, false, now);
  }
  // 走到了、歇够了、跳完了
  function decide(now) {
    if (still.matches) return setMode('rest', now, [60000, 120000]);
    if (st.edge === 'floor') return onFloor(now);
    if (st.edge === 'ceiling') return onCeiling(now);
    return onWall(now);
  }
  function onFloor(now) {
    const span = hi('floor') - lo('floor');
    if (now >= st.climbAt && room.bottom - room.top > W * 4 && Math.random() < 0.4) {
      // 去爬墙：多半去近的那一边
      const nearRight = hi('floor') - st.s < st.s - lo('floor');
      const right = Math.random() < 0.7 ? nearRight : !nearRight;
      return go(right ? hi('floor') : lo('floor'), speed, true, now);
    }
    const roll = Math.random();
    if (roll < 0.5) {
      let spot = lo('floor') + Math.random() * span;
      if (Math.abs(spot - st.s) < 60) spot = st.s < lo('floor') + span / 2 ? st.s + 120 : st.s - 120;   // 别原地抽筋
      return go(spot, speed, false, now);
    }
    if (roll < 0.72) return setMode('dance', now);
    return setMode('rest', now);
  }
  function onWall(now) {
    if (st.plan === 'cling') { st.plan = 'decide'; return setMode('rest', now, CLING); }
    if (st.plan === 'drop') return letGo(now);
    st.plan = '';
    const roll = Math.random();
    if (roll < 0.42) return go(lo(st.edge), CLIMB, true, now);    // 接着爬到顶上
    if (roll < 0.78) return go(hi(st.edge), CLIMB, true, now);    // 爬回底下
    return letGo(now);
  }
  function onCeiling(now) {
    if (st.plan === 'hang') { st.plan = 'decide'; return setMode('rest', now, HANG); }
    st.plan = '';
    if (Math.random() < 0.5) return letGo(now);
    // 接着往前走到头，从那边的墙爬下去
    return go(st.dir * ALONG.ceiling > 0 ? hi('ceiling') : lo('ceiling'), CEIL, true, now);
  }
  function letGo(now) {
    st.plan = '';
    setMode('wobble', now);           // 先晃两下（style.css 的 is-slip），晃完松手
  }

  // ------------------------------------------------------------ 往下掉：自己松手、被扔出去、刚出来从顶上掉下来
  function startFall(vx, vy, thrown, now) {
    const c = centre();
    st.cx = c[0];
    st.cy = c[1];
    st.vx = vx;
    st.vy = vy;
    st.thrown = thrown;
    st.plan = '';
    st.turn = null;
    setMode('fall', now);
  }
  function fallStep(dt, now) {
    st.vy = Math.min(st.vy + G * dt, MAX_FALL);
    st.cx += st.vx * dt;
    st.cy += st.vy * dt;
    const upright = Math.round(st.angle / 360) * 360;    // 翻个身，脚朝下
    st.angle += (upright - st.angle) * Math.min(1, dt * 9);
    const r = st.angle * Math.PI / 180, cos = Math.abs(Math.cos(r)), sin = Math.abs(Math.sin(r));
    const hx = cos * W / 2 + sin * H / 2, hy = cos * H / 2 + sin * W / 2;   // 转着的身子占多宽多高的一半
    if (st.cx - hx < room.left) { st.cx = room.left + hx; st.vx = Math.abs(st.vx) * 0.5; }
    else if (st.cx + hx > room.right) { st.cx = room.right - hx; st.vx = -Math.abs(st.vx) * 0.5; }
    if (st.cy - hy < room.top && st.vy < 0) { st.cy = room.top + hy; st.vy = -st.vy * 0.35; }
    let landed = false;
    if (st.cy + hy >= room.bottom) {
      st.cy = room.bottom - hy;
      if (st.vy > BOUNCE_MIN) {
        st.vy = -st.vy * BOUNCE;
        st.vx *= 0.7;
        squash();
      } else {
        landed = true;
      }
    }
    const rr = st.angle * Math.PI / 180;
    st.x = st.cx - Math.sin(rr) * H / 2;
    st.y = st.cy + Math.cos(rr) * H / 2;
    if (landed) land(now);
  }
  function land(now) {
    st.edge = 'floor';
    st.angle = 0;
    st.vx = 0;
    st.vy = 0;
    st.s = clamp(st.cx, lo('floor'), hi('floor'));
    toFoot();
    squash();
    st.climbAt = Math.max(st.climbAt, now + rand(CLIMB_EVERY) / 2);
    setMode('rest', now, [700, 1600]);
    if (st.arriving) speak('arrive', 2200);
    else if (st.thrown) speak('landed', 1800);
    st.arriving = false;
    st.thrown = false;
  }

  // ------------------------------------------------------------ 说话：对话框摆在它头朝的那一边
  const recent = [];                  // 最近说过的先不说
  let hideTimer = null;
  function pick(pool) {
    if (!pool || !pool.length) return '';
    const fresh = pool.filter(function (said) { return recent.indexOf(said) < 0; });
    const from = fresh.length ? fresh : pool;
    return from[Math.floor(Math.random() * from.length)];
  }
  function lineFor(kind) {
    if (kind !== 'hover' && kind !== 'poke') return pick(talk[kind]);
    if (kind === 'poke' && Math.random() < 0.55) return pick(talk.poked);
    if (st.edge === 'ceiling' && Math.random() < 0.5) return pick(talk.ceiling);
    if (!flat(st.edge) && Math.random() < 0.5) return pick(talk.climb);
    const roll = Math.random();
    let said = '';
    if (roll < 0.36) said = pick(talk.page);
    else if (roll < 0.52) said = pick(talk.context);
    else if (roll < 0.74) said = pick(talk.moods);
    return said || pick(talk.shared);
  }
  // ms：说多久自己收起来（不给就一直在，鼠标移开再收）；chance：有几成会说
  function speak(kind, ms, chance) {
    if (chance !== undefined && Math.random() >= chance) return;
    const said = lineFor(kind);
    if (!said) return;
    recent.push(said);
    if (recent.length > 12) recent.shift();
    clearTimeout(hideTimer);
    bubble.textContent = said;
    bubble.hidden = false;
    bubble.style.animation = 'none';  // 换一句也从头冒一下
    void bubble.offsetWidth;
    bubble.style.animation = '';
    placeBubble();
    if (ms) hideTimer = setTimeout(hideBubble, ms);
  }
  function hideSoon(ms) {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hideBubble, ms);
  }
  function hideBubble() { bubble.hidden = true; }
  function placeBubble() {
    if (bubble.hidden) return;
    const box = pet.getBoundingClientRect();
    const bw = bubble.offsetWidth, bh = bubble.offsetHeight;
    const vw = layer.clientWidth, vh = layer.clientHeight;
    const cx = box.left + box.width / 2, cy = box.top + box.height / 2;
    let side = 'above';
    if (settled()) side = st.edge === 'ceiling' ? 'below' : st.edge === 'left' ? 'right' : st.edge === 'right' ? 'left' : 'above';
    if (side === 'above' && box.top - bh - 10 < 6) side = 'below';
    else if (side === 'right' && box.right + bw + 10 > vw - 6) side = 'left';
    else if (side === 'left' && box.left - bw - 10 < 6) side = 'right';
    let left, top;
    if (side === 'above' || side === 'below') {
      left = cx - bw / 2;
      top = side === 'above' ? box.top - bh - 10 : box.bottom + 10;
    } else {
      left = side === 'right' ? box.right + 10 : box.left - bw - 10;
      top = cy - bh / 2;
    }
    left = clamp(left, 6, Math.max(6, vw - bw - 6));
    top = clamp(top, 6, Math.max(6, vh - bh - 6));
    const tail = side === 'above' || side === 'below' ? clamp(cx - left, 12, bw - 12) : clamp(cy - top, 10, bh - 10);
    bubble.dataset.side = side;
    bubble.style.setProperty('--tail', Math.round(tail) + 'px');
    bubble.style.transform = 'translate(' + Math.round(left) + 'px, ' + Math.round(top) + 'px)';
  }

  // ------------------------------------------------------------ 鼠标放上去说话，点一下蹦一下
  let pokeTimer = null;
  function hold() {
    if (hovered || gone || !settled() || press) return;
    hovered = true;
    sync();
    speak('hover');
  }
  function release() {
    clearTimeout(pokeTimer);
    if (!hovered) return;
    hovered = false;
    hideSoon(900);
    if (st.mode !== 'walk') st.until = Math.max(st.until, performance.now() + 700);
    sync();
  }
  pet.addEventListener('pointerenter', function (event) { if (event.pointerType === 'mouse') hold(); });
  pet.addEventListener('pointerleave', function (event) { if (event.pointerType === 'mouse') release(); });
  pet.addEventListener('focus', function () { if (pet.matches(':focus-visible')) hold(); });
  pet.addEventListener('blur', release);
  pet.addEventListener('click', function () {
    if (swallowClick || gone || !settled()) return;
    if (!hovered) {
      // 手机上没有悬浮：点一下停下来说一句，过一会儿接着走
      hovered = true;
      sync();
      clearTimeout(pokeTimer);
      pokeTimer = setTimeout(release, 2600);
    }
    speak('poke');
    hop();
  });

  // ------------------------------------------------------------ 按住拖、松手扔
  function velocity(trail, now, within) {
    const fresh = trail.filter(function (point) { return now - point[0] <= within; });
    if (fresh.length < 2) return [0, 0];
    const a = fresh[0], b = fresh[fresh.length - 1], span = (b[0] - a[0]) / 1000;
    return span > 0.01 ? [(b[1] - a[1]) / span, (b[2] - a[2]) / span] : [0, 0];
  }
  function pickUp(event) {
    const now = performance.now();
    // 抓在它身上哪一点：换成它自己的坐标（脚底中间是原点、没转过的样子），拖的时候这一点一直在指针底下
    const r = -st.angle * Math.PI / 180, dx = press.x - st.x, dy = press.y - st.y;
    drag = { gx: dx * Math.cos(r) - dy * Math.sin(r), gy: dx * Math.sin(r) + dy * Math.cos(r),
             px: event.clientX, py: event.clientY, trail: [], base: Math.round(st.angle / 360) * 360 };
    clearTimeout(pokeTimer);
    hovered = false;
    st.turn = null;
    st.plan = '';
    setMode('drag', now);
    speak('held');
  }
  function dragStep(dt, now) {
    // 跟着晃：往右拖，身子往左甩（绕着抓的那一点转），停下来慢慢摆正
    const v = velocity(drag.trail, now, 120);
    const want = drag.base + clamp(v[0] * 0.03, -40, 40);
    st.angle += (want - st.angle) * Math.min(1, dt * 10);
    const r = st.angle * Math.PI / 180;
    st.x = drag.px - (drag.gx * Math.cos(r) - drag.gy * Math.sin(r));
    st.y = drag.py - (drag.gx * Math.sin(r) + drag.gy * Math.cos(r));
  }
  function letDrop(event) {
    if (!press || event.pointerId !== press.id) return;
    press = null;
    if (!drag) return;
    const now = performance.now();
    let v = velocity(drag.trail, now, 90);
    const fast = Math.hypot(v[0], v[1]);
    if (fast > THROW_MAX) v = [v[0] * THROW_MAX / fast, v[1] * THROW_MAX / fast];
    drag = null;
    swallowClick = true;
    setTimeout(function () { swallowClick = false; }, 0);
    hideSoon(500);
    if (!still.matches) return startFall(v[0], v[1], true, now);
    st.cx = centre()[0];              // 减少动态效果：直接落在底下
    land(now);
  }
  pet.addEventListener('pointerdown', function (event) {
    if (event.button !== 0 || gone) return;
    closeMenu(false);
    press = { id: event.pointerId, x: event.clientX, y: event.clientY };
    try { pet.setPointerCapture(event.pointerId); } catch (error) { /* 拖出窗口就跟丢，不要紧 */ }
  });
  pet.addEventListener('pointermove', function (event) {
    if (!press || event.pointerId !== press.id) return;
    if (!drag) {
      if (Math.hypot(event.clientX - press.x, event.clientY - press.y) < 6) return;
      pickUp(event);
    }
    drag.px = event.clientX;
    drag.py = event.clientY;
    drag.trail.push([event.timeStamp, event.clientX, event.clientY]);
    if (drag.trail.length > 12) drag.trail.shift();
  });
  pet.addEventListener('pointerup', letDrop);
  pet.addEventListener('pointercancel', letDrop);

  // ------------------------------------------------------------ 眼睛跟着鼠标；冲过来又快又近，吓一跳
  let mouse = null;
  let looking = false, lookX = 0, lookY = 0;
  document.addEventListener('pointermove', function (event) {
    if (event.pointerType !== 'mouse') return;
    const t = event.timeStamp;
    if (!mouse) {
      mouse = { x: event.clientX, y: event.clientY, t: t, vx: 0, vy: 0 };
      return;
    }
    const dt = Math.max(0.008, (t - mouse.t) / 1000);
    mouse.vx += 0.35 * ((event.clientX - mouse.x) / dt - mouse.vx);
    mouse.vy += 0.35 * ((event.clientY - mouse.y) / dt - mouse.vy);
    mouse.x = event.clientX;
    mouse.y = event.clientY;
    mouse.t = t;
  }, { passive: true });
  document.documentElement.addEventListener('mouseleave', function () { mouse = null; });
  function look(now) {
    let lx = 0, ly = 0, near = false;
    if (mouse && st.mode !== 'drag' && now - mouse.t < 6000) {
      const r = st.angle * Math.PI / 180, cos = Math.cos(r), sin = Math.sin(r);
      const eye = 6.5 * H / 8;        // 眼睛那一行在脚底上面 6.5 格
      const dx = mouse.x - (st.x + eye * sin), dy = mouse.y - (st.y - eye * cos);
      const dist = Math.hypot(dx, dy);
      if (dist < LOOK_NEAR) {
        near = true;
        // 换成它自己的坐标：转回去；往左走时整个是翻过来的，眼睛也得反着挪
        const ux = (dx * cos + dy * sin) * st.dir, uy = -dx * sin + dy * cos;
        lx = Math.abs(ux) > 14 ? Math.sign(ux) : 0;
        ly = Math.abs(uy) > 20 ? Math.sign(uy) : 0;
        if (dist < DODGE_NEAR) dodge(now, dx, dy, dist);
      }
    }
    if (near !== looking) { looking = near; pet.classList.toggle('is-looking', near); }
    if (lx !== lookX) { lookX = lx; pet.style.setProperty('--lx', String(lx)); }
    if (ly !== lookY) { lookY = ly; pet.style.setProperty('--ly', String(ly)); }
  }
  function dodge(now, dx, dy, dist) {
    if (now < st.dodgeAt || now - mouse.t > 60 || hovered || press || st.edge !== 'floor' || !settled()
        || still.matches || !menu.hidden) return;
    const fast = Math.hypot(mouse.vx, mouse.vy);
    const toward = -(dx * mouse.vx + dy * mouse.vy) / (dist || 1);    // 冲着它来的那部分速度
    if (fast < DODGE_SPEED || toward < fast * 0.5) return;
    st.dodgeAt = now + DODGE_GAP;
    if (Math.random() < 0.35) return;   // 不是每次都被吓到
    hop();
    speak('dodge', 1400);
    go(st.s + (dx > 0 ? -1 : 1) * rand([110, 170]), RUN, false, now);   // 鼠标在右边就往左跑
  }

  // ------------------------------------------------------------ 右键：「收起小家伙」
  const offButton = menu.querySelector('[data-pet-off]');
  function openMenu(x, y) {
    menu.hidden = false;
    const w = menu.offsetWidth, h = menu.offsetHeight, vw = layer.clientWidth, vh = layer.clientHeight;
    const left = clamp(x + 4, 6, Math.max(6, vw - w - 6));
    const top = clamp(y - h - 4 < 6 ? y + 4 : y - h - 4, 6, Math.max(6, vh - h - 6));   // 多半在它上面：它多半在底下
    menu.style.transform = 'translate(' + Math.round(left) + 'px, ' + Math.round(top) + 'px)';
    hideBubble();
    sync();
    if (offButton) offButton.focus({ preventScroll: true });
  }
  function closeMenu(refocus) {
    if (menu.hidden) return;
    menu.hidden = true;
    sync();
    if (refocus) pet.focus({ preventScroll: true });
  }
  pet.addEventListener('contextmenu', function (event) {
    event.preventDefault();
    if (gone) return;
    const box = pet.getBoundingClientRect();
    // 键盘上的菜单键没有坐标：开在它头上
    openMenu(event.clientX || box.left + box.width / 2, event.clientY || box.top);
  });
  document.addEventListener('pointerdown', function (event) {
    if (!menu.hidden && !menu.contains(event.target)) closeMenu(false);
  }, true);
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && !menu.hidden) closeMenu(true);
  });
  menu.addEventListener('focusout', function (event) {
    if (!menu.contains(event.relatedTarget)) closeMenu(false);
  });
  window.addEventListener('blur', function () { closeMenu(false); });
  function goodbye() {
    gone = true;
    try { sessionStorage.removeItem(KEY); } catch (error) { /* 存不了就算了 */ }
    pet.classList.remove('is-walking', 'is-dancing', 'is-looking');
    pet.classList.add('is-talking');  // 举手拜拜
    speak('bye');
    if (window.appToast) window.appToast('info', '小家伙收起来了', '哪个页面都不出来了；想它了去「我的资料」里打开。');
    setTimeout(function () { layer.classList.add('is-leaving'); bubble.hidden = true; }, 1600);
    setTimeout(function () { layer.remove(); }, 2200);
  }
  if (offButton) {
    offButton.addEventListener('click', function () {
      closeMenu(false);
      const body = new FormData();
      body.append('csrf', conf.csrf);
      body.append('pet', 'off');
      fetch(conf.url, { method: 'POST', body: body, headers: { 'X-Requested-With': 'fetch' }, credentials: 'same-origin' })
        .then(function (response) { return response.json(); })
        .then(function (result) {
          if (!result.ok) throw new Error('pet');
          goodbye();
        })
        .catch(function () {
          if (window.appToast) window.appToast('error', '小家伙没收起来', '页面可能已经过期，刷新后再试。');
        });
    });
  }

  // ------------------------------------------------------------ 换页面接着在原来的地方
  function save() {
    if (gone) return;
    let edge = st.edge, at = st.s, walking = st.mode === 'walk';
    if (st.mode === 'turn' && st.turn) {
      edge = st.turn.edge;
      at = st.turn.s;
      walking = false;
    } else if (!settled() || st.mode === 'wobble') {    // 半空中、被拎着、正要松手：下一页落在它底下
      edge = 'floor';
      at = st.mode === 'fall' ? st.cx : st.x;
      walking = false;
    }
    const span = hi(edge) - lo(edge);
    const frac = function (value) { return span > 0 ? clamp((value - lo(edge)) / span, 0, 1) : 0.5; };
    const data = { edge: edge, at: frac(at), dir: st.dir, plan: edge === st.edge ? st.plan : '',
                   walk: walking ? { to: frac(st.target), pass: st.pass, pace: st.pace } : null,
                   climbIn: Math.max(0, Math.round(st.climbAt - performance.now())) };
    try { sessionStorage.setItem(KEY, JSON.stringify(data)); } catch (error) { /* 存不了：下一页从顶上掉下来 */ }
  }
  function restore(now) {
    let saved = null;
    try { saved = JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch (error) { saved = null; }
    if (!saved || LOOP.indexOf(saved.edge) < 0) return false;
    st.edge = still.matches ? 'floor' : saved.edge;
    const span = hi(st.edge) - lo(st.edge);
    st.s = lo(st.edge) + clamp(Number(saved.at) || 0, 0, 1) * span;
    st.dir = saved.dir === 1 ? 1 : -1;
    st.angle = BASE[st.edge];
    st.plan = typeof saved.plan === 'string' ? saved.plan : '';
    st.climbAt = now + clamp(Number(saved.climbIn) || 0, 0, CLIMB_EVERY[1]);
    toFoot();
    const walk = saved.walk;
    if (walk && !still.matches && st.edge === saved.edge) {
      st.target = lo(st.edge) + clamp(Number(walk.to) || 0, 0, 1) * span;
      st.pass = !!walk.pass;
      st.pace = clamp(Number(walk.pace) || speed, 10, RUN);
      setMode('walk', now);
    } else {
      setMode('rest', now, [300, 1200]);
    }
    return true;
  }
  // 这个标签页里第一次出来：从顶上掉下来（减少动态效果就直接站在右下角）
  function arriveFirst(now) {
    st.edge = 'floor';
    st.angle = 0;
    st.dir = Math.random() < 0.5 ? 1 : -1;
    st.climbAt = now + rand(FIRST_CLIMB);
    if (still.matches) {
      st.s = lo('floor') + 0.85 * (hi('floor') - lo('floor'));
      toFoot();
      setMode('rest', now, [60000, 120000]);
      return;
    }
    st.x = room.left + (room.right - room.left) * rand([0.55, 0.9]);
    st.y = room.top - 2;              // 脚在窗口上沿：整只还在外面（图层裁掉），掉进来
    st.arriving = true;
    startFall(0, 0, false, now);
  }

  // ------------------------------------------------------------ 一帧一帧推着走
  let drawn = '', drawnDir = 0;
  function render() {
    const exact = settled() && st.angle % 90 === 0;      // 停在边上：对齐整像素，像素画才不糊
    const x = st.x - W / 2, y = st.y - H;
    const value = 'translate(' + (exact ? Math.round(x) : x.toFixed(1)) + 'px, ' + (exact ? Math.round(y) : y.toFixed(1))
      + 'px) rotate(' + Number(st.angle.toFixed(2)) + 'deg)';
    if (value !== drawn) {
      drawn = value;
      pet.style.transform = value;
    }
    if (st.dir !== drawnDir) {
      drawnDir = st.dir;
      pet.style.setProperty('--dir', String(st.dir));
    }
  }
  let last = performance.now();
  function frame(now) {
    if (gone) return;
    const dt = Math.min(0.05, Math.max(0, (now - last) / 1000));   // 页面从后台回来时别一下子瞬移
    last = now;
    if (st.mode === 'drag') dragStep(dt, now);
    else if (st.mode === 'fall') fallStep(dt, now);
    else if (st.mode === 'turn') turnStep(now);
    else if (!hovered && menu.hidden) {
      if (st.mode === 'walk') walkStep(dt, now);
      else if (now >= st.until) {
        if (st.mode === 'wobble') {
          speak('fall', 1100, 0.6);
          startFall(rand([-80, 80]), 0, false, now);
        } else {
          decide(now);
        }
      }
    }
    look(now);
    render();
    placeBubble();
    window.requestAnimationFrame(frame);
  }

  layer.hidden = false;               // 先露出来才量得出多大；同一个任务里放好，不会先在左上角闪一下
  measure();
  const start = performance.now();
  if (!restore(start)) arriveFirst(start);
  render();
  sync();
  window.requestAnimationFrame(function (now) { last = now; frame(now); });
  // 窗口变了、侧边栏收起展开：重新量，马上挪过去（不等下一帧：后台标签里帧会停）
  function remeasure() {
    measure();
    render();
    placeBubble();
  }
  window.addEventListener('resize', function () { closeMenu(false); remeasure(); });
  if (window.ResizeObserver && sidebar) new ResizeObserver(remeasure).observe(sidebar);
  if (wide.addEventListener) wide.addEventListener('change', remeasure);
  window.addEventListener('pagehide', save);
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') save(); });
})();
