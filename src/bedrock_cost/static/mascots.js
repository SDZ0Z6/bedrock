/* 概览顶上的像素小家伙和它们的小风景（templates/_mascot.html、welcome.py）。
 *
 * 小家伙：在台子上走来走去、停一会儿、跳一会儿舞，随机安排。鼠标移上去（或者用键盘移到它身上）它就停下来举手，
 * 头上冒个对话框说一句；点一下蹦一下、换一句。说什么从 #mascot-talk 里挑：一多半说自己脾气那一类（data-mood），
 * 有时候说看场合的（叫名字、看几点、看概览上的数），有时候说跟着背景的（下雨、下雪……），有时候说大家都会说的；
 * 被点了多半是「哎哟」那一类。最近说过的十句先不说，免得几只轮着说同一句。
 * 不用等人：隔半分钟上下挑一只自己说一句（停下来举手，说三秒）；已经有对话框开着（这里的、页面底下那只的）、
 * 页面在后台、欢迎条滚出屏幕时这一回不说。
 *
 * 风景跟着它们动：
 *   · 视差：按几只小家伙站的地方算一个「镜头」（--cam，-0.5 ~ 0.5，慢慢追过去），天、远山、树三层各按自己的
 *     深度挪（style.css）——越远的挪得越多，草地不动（小家伙站在上面）；
 *   · 踩草：小家伙脚底下的草和花弯下去，往它走的方向倒，走开就弹回来；
 *   · 树：有小家伙走到树底下，树冠晃一晃、掉一片叶子；
 *   · 扬灰：走路时脚后面扬一点小灰。
 *
 * 背景（welcome.SCENES）：欢迎条上的 data-kind 是现在画哪个，颜色、哪样露出来、小道具（伞、围巾、头上的花瓣）都是
 * style.css 按它换的；这里管天上掉下来的（雨、雪、花瓣、落叶、萤火虫，撒在 .scene-weather 里），夜里让一只睡着
 * （冒 Zzz、说梦话），下雪走着走着会打滑，下雨多半往树底下躲。右上角的按钮点开换背景：马上换，不刷新页面，
 * 挑的记在自己的资料里（POST /me/scene，#scene-conf 里有网址和令牌）。
 *
 * 系统设了「减少动态效果」：都站着不动，风景也不动，不撒雨雪（CSS 也把动画关了）；对话框、换背景照样有。
 * 页面放到后台时 rAF 自己会停。 */
(function () {
  'use strict';
  const stage = document.querySelector('[data-mascot-stage]');
  const source = document.getElementById('mascot-talk');
  if (!stage || !source) return;
  const talk = JSON.parse(source.textContent);
  const still = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
  const SPEED = [24, 46];        // 每秒走多少像素（每只随机一个）
  const DANCE = [2200, 4800];    // 跳多久（毫秒）
  const REST = [900, 2600];      // 站多久
  const PUFF_EVERY = 280;        // 走路时多久扬一次灰（毫秒）
  const SLIP_RATE = 0.3;         // 雪地上走着，平均每秒滑几跤
  const SLEEPY_KINDS = ['night', 'fireflies'];
  const rand = function (range) { return range[0] + Math.random() * (range[1] - range[0]); };

  const crew = Array.prototype.map.call(stage.querySelectorAll('[data-mascot]'), function (el) {
    return { el: el, bubble: el.querySelector('[data-bubble]'), mood: el.dataset.mood, x: 0, target: 0, dir: 1,
             speed: rand(SPEED), mode: 'rest', until: 0, held: false, hideTimer: null, puffAt: 0,
             sleeping: false, slipping: false, chatting: false };
  });
  if (!crew.length) return;
  // 夜里睡着的那只：卖萌的那只（没有就第一只）
  const sleeper = crew.find(function (m) { return m.mood === 'cute'; }) || crew[0];

  // 宽屏上整条欢迎条大一号（style.css 的 --zoom、--pan）：走得快一点、镜头挪得多一点，看着和小屏上一个节奏
  let zoom = 1, pan = 56, cam = 0;
  function readScale() {
    const css = window.getComputedStyle(stage);
    zoom = parseFloat(css.getPropertyValue('--zoom')) || 1;
    pan = parseFloat(css.getPropertyValue('--pan')) || 56;
  }
  readScale();
  const room = function (m) { return Math.max(0, stage.clientWidth - m.el.offsetWidth); };
  const middle = function (m) { return m.x + m.el.offsetWidth / 2; };
  function put(m) {
    m.el.style.transform = 'translateX(' + Math.round(m.x) + 'px)';
    m.el.style.setProperty('--dir', String(m.dir));
  }
  function setMode(m, mode, now) {
    m.mode = mode;
    m.el.classList.toggle('is-walking', mode === 'walk');
    m.el.classList.toggle('is-dancing', mode === 'dance');
    if (mode === 'dance') m.until = now + rand(DANCE);
    if (mode === 'rest') m.until = now + rand(REST);
  }

  // ---- 风景里的东西：草和树在台子上的位置（窗口变了重新量）
  const scene = stage.querySelector('[data-scene]');
  const weatherBox = scene ? scene.querySelector('[data-weather]') : null;
  const puffs = scene ? scene.querySelector('[data-puffs]') : null;
  const mid = scene ? scene.querySelector('[data-layer="mid"]') : null;
  const tufts = scene ? Array.prototype.map.call(scene.querySelectorAll('[data-tuft]'), function (el) {
    return { el: el, x: 0, pressed: false };
  }) : [];
  const trees = scene ? Array.prototype.map.call(scene.querySelectorAll('[data-tree]'), function (el) {
    return { el: el, x: 0, half: 0, under: [], calmUntil: 0 };
  }) : [];
  const RUSTLE_GAP = 1200;    // 一棵树晃完至少歇这么久：站在树边上的小家伙一进一出，别让它一直掉叶子
  const MID_DEPTH = 0.4;      // 和 style.css 里 .scene-mid 的深度一样
  function measure() {
    for (const tuft of tufts) tuft.x = tuft.el.offsetLeft + tuft.el.offsetWidth / 2;
    for (const tree of trees) {
      tree.x = (mid ? mid.offsetLeft : 0) + tree.el.offsetLeft + tree.el.offsetWidth / 2;
      tree.half = tree.el.offsetWidth / 2;
    }
  }
  measure();
  const treeCentre = function (tree) { return tree.x + cam * pan * MID_DEPTH; };

  // 走去哪：随便挑几个地方，选离别的小家伙（站着的地方、正要去的地方）最远的那个，免得几只挤成一团；
  // 至少挪开一段，不然看着像原地抽筋。下雨的时候多半往树底下躲
  function destination(m) {
    const width = room(m);
    const others = [];
    for (const other of crew) {
      if (other === m) continue;
      others.push(other.x);
      if (other.mode === 'walk') others.push(other.target);
    }
    function clear(spot) {     // 离最近的那只（站着的、正走过去的）有多远
      return others.length ? Math.min.apply(null, others.map(function (x) { return Math.abs(x - spot); })) : width;
    }
    let best = m.x, score = -1;
    if (stage.dataset.kind === 'rain' && trees.length && Math.random() < 0.6) {
      for (const tree of trees) {     // 往树底下躲，挑还没人躲的那棵
        const spot = treeCentre(tree) - m.el.offsetWidth / 2 + (Math.random() - 0.5) * tree.half * 0.6;
        const fit = Math.min(Math.max(spot, 0), width);
        if (clear(fit) > score) { best = fit; score = clear(fit); }
      }
      return best;
    }
    for (let tries = 0; tries < 5; tries++) {
      let spot = Math.random() * width;
      if (Math.abs(spot - m.x) < 40 * zoom) spot = m.x < width / 2 ? Math.min(width, m.x + 90 * zoom) : Math.max(0, m.x - 90 * zoom);
      if (clear(spot) > score) { best = spot; score = clear(spot); }
    }
    return best;
  }
  function next(m, now) {
    const roll = Math.random();
    if (roll < 0.5) {
      const target = destination(m);
      m.target = target;
      m.dir = target >= m.x ? 1 : -1;
      put(m);
      setMode(m, 'walk', now);
    } else if (roll < 0.8) {
      setMode(m, 'dance', now);
    } else {
      setMode(m, 'rest', now);
    }
  }

  // 一开始按 data-start 站好（0 最左、1 最右），朝向随便
  for (const m of crew) {
    m.x = Number(m.el.dataset.start || 0) * room(m);
    m.dir = Math.random() < 0.5 ? 1 : -1;
    put(m);
  }

  // ---- 说什么：最近说过的先不说
  const recent = [];
  function pick(pool) {
    if (!pool || !pool.length) return '';
    const fresh = pool.filter(function (line) { return recent.indexOf(line) < 0; });
    const from = fresh.length ? fresh : pool;
    return from[Math.floor(Math.random() * from.length)];
  }
  function lineFor(m, poked) {
    if (m.sleeping) return pick(talk.sleepy);
    if (poked && Math.random() < 0.6) return pick(talk.poked);
    const roll = Math.random();
    let line = '';
    if (roll < 0.2) line = pick(talk.context);
    else if (roll < 0.42) line = pick((talk.scenes || {})[stage.dataset.kind]);
    else if (roll < 0.78) line = pick(talk.moods[m.mood]);
    return line || pick(talk.shared);
  }

  // ---- 对话框：靠卡片边上时往里挪（--shift），别伸出去被裁掉
  function say(m, poked) {
    const line = lineFor(m, poked);
    recent.push(line);
    if (recent.length > 10) recent.shift();
    clearTimeout(m.hideTimer);
    m.bubble.textContent = line;
    m.bubble.style.setProperty('--shift', '0px');
    m.bubble.hidden = false;
    const box = m.bubble.getBoundingClientRect();
    const edge = (stage.closest('.welcome') || stage).getBoundingClientRect();
    let shift = 0;
    if (box.left < edge.left + 8) shift = edge.left + 8 - box.left;
    else if (box.right > edge.right - 8) shift = edge.right - 8 - box.right;
    m.bubble.style.setProperty('--shift', Math.round(shift) + 'px');
  }
  function hold(m) {
    if (m.held) return;
    m.held = true;
    m.chatting = false;
    if (!m.sleeping) {          // 睡着的那只不举手，说梦话
      m.el.classList.remove('is-walking', 'is-dancing');
      m.el.classList.add('is-talking');
    }
    say(m, false);
  }
  function release(m) {
    if (!m.held) return;
    m.held = false;
    m.el.classList.remove('is-talking');
    clearTimeout(m.hideTimer);
    m.hideTimer = setTimeout(function () { m.bubble.hidden = true; }, 900);
    if (!m.sleeping) setMode(m, 'rest', performance.now());
  }
  for (const m of crew) {
    m.el.addEventListener('mouseenter', function () { hold(m); });
    m.el.addEventListener('mouseleave', function () { release(m); });
    m.el.addEventListener('focus', function () { hold(m); });
    m.el.addEventListener('blur', function () { release(m); });
    m.el.addEventListener('click', function () {
      if (m.held) say(m, true); else hold(m);
      if (still.matches || m.sleeping) return;
      m.el.classList.remove('is-hop');
      void m.el.offsetWidth;   // 去掉再加上，动画才会重新放一遍
      m.el.classList.add('is-hop');
    });
    m.el.addEventListener('animationend', function (event) {
      if (event.animationName === 'mascot-hop') m.el.classList.remove('is-hop');
      if (event.animationName === 'mascot-slip') {
        m.el.classList.remove('is-slip');
        m.slipping = false;
      }
    });
  }

  // ---- 不用等人，自己说一句：隔 CHAT_EVERY 挑一只没人理的，停下来举手说 CHAT_FOR（睡着的那只说梦话，不举手）
  const CHAT_EVERY = [22000, 45000];
  const CHAT_FOR = 3200;
  function chatter() {
    window.setTimeout(chatter, rand(CHAT_EVERY));
    if (document.hidden || document.querySelector('.mascot-bubble:not([hidden]), .pet-bubble:not([hidden])')) return;
    const box = stage.getBoundingClientRect();
    if (box.bottom < 0 || box.top > window.innerHeight) return;
    const idle = crew.filter(function (m) { return !m.held; });
    if (!idle.length) return;
    const m = idle[Math.floor(Math.random() * idle.length)];
    m.chatting = true;
    if (!m.sleeping) {
      m.el.classList.remove('is-walking', 'is-dancing');
      m.el.classList.add('is-talking');
    }
    say(m, false);
    m.hideTimer = window.setTimeout(function () {
      m.chatting = false;
      if (m.held) return;
      m.el.classList.remove('is-talking');
      m.bubble.hidden = true;
      if (!m.sleeping) setMode(m, 'rest', performance.now());
    }, CHAT_FOR);
  }
  window.setTimeout(chatter, rand([8000, 16000]));

  // ---- 天上掉下来的：按背景撒一把（多少跟着台子宽窄），各自随机位置、快慢、从半路开始
  const WEATHER = {
    rain: { cls: 'wx-drop', every: 11, time: [0.55, 0.95] },
    snow: { cls: 'wx-flake', every: 24, time: [5, 9] },
    sakura: { cls: 'wx-petal', every: 46, time: [6, 10] },
    autumn: { cls: 'wx-leaf', every: 52, time: [6, 11], colors: ['#e08a4f', '#d36a47', '#e2b450', '#c9783f'] },
    fireflies: { cls: 'wx-firefly', every: 54, time: [2.6, 5], low: true },
  };
  function weather(kind) {
    if (!weatherBox) return;
    weatherBox.replaceChildren();
    const spec = WEATHER[kind];
    if (!spec || still.matches) return;
    const count = Math.max(6, Math.round((stage.clientWidth || 400) / spec.every));
    for (let i = 0; i < count; i++) {
      const bit = document.createElement('i');
      const time = rand(spec.time);
      bit.className = 'wx ' + spec.cls;
      bit.style.left = (Math.random() * 108 - 4).toFixed(1) + '%';
      if (spec.low) {
        // 萤火虫：在草丛那一截飘来飘去，一闪一闪
        bit.style.top = (40 + Math.random() * 50).toFixed(1) + '%';
        bit.style.setProperty('--fx', ((Math.random() < 0.5 ? -1 : 1) * rand([10, 32])).toFixed(0) + 'px');
        bit.style.setProperty('--fy', ((Math.random() < 0.5 ? -1 : 1) * rand([6, 20])).toFixed(0) + 'px');
        bit.style.animationDuration = time.toFixed(2) + 's, ' + rand([1.6, 3.2]).toFixed(2) + 's';
        bit.style.animationDelay = (-Math.random() * time).toFixed(2) + 's, ' + (-Math.random() * 3).toFixed(2) + 's';
      } else {
        bit.style.animationDuration = time.toFixed(2) + 's';
        bit.style.animationDelay = (-Math.random() * time).toFixed(2) + 's';
      }
      if (spec.colors) bit.style.setProperty('--leaf', spec.colors[i % spec.colors.length]);
      weatherBox.append(bit);
    }
  }

  // ---- 换背景：颜色、小道具由 CSS 按 data-kind 换；这里撒雨雪、让夜里那只睡着（换走了就醒）
  function applyKind(kind) {
    stage.dataset.kind = kind;
    weather(kind);
    const now = performance.now();
    for (const m of crew) {
      const sleep = SLEEPY_KINDS.indexOf(kind) >= 0 && m === sleeper;
      if (sleep === m.sleeping) continue;
      m.sleeping = sleep;
      m.slipping = false;
      m.el.classList.toggle('is-sleeping', sleep);
      m.el.classList.remove('is-walking', 'is-dancing', 'is-slip');
      setMode(m, 'rest', now);
    }
  }
  applyKind(stage.dataset.kind || 'day');

  // 右上角的按钮：点开一排选项（原生 popover），选了马上换、记在自己的资料里；选「自动」就换成 data-auto 那个
  const confBox = document.getElementById('scene-conf');
  const menu = document.getElementById('scene-menu');
  const opener = stage.querySelector('[data-scene-open]');
  const label = stage.querySelector('[data-scene-label]');
  if (confBox && menu && opener) {
    const conf = JSON.parse(confBox.textContent);
    const place = function () {
      const box = opener.getBoundingClientRect();
      menu.style.top = Math.round(box.bottom + 6) + 'px';
      menu.style.right = Math.round(Math.max(8, document.documentElement.clientWidth - box.right)) + 'px';
    };
    opener.addEventListener('click', place);
    menu.addEventListener('beforetoggle', function (event) { if (event.newState === 'open') place(); });
    menu.addEventListener('click', function (event) {
      const button = event.target instanceof Element ? event.target.closest('[data-scene-pick]') : null;
      if (!button) return;
      const chosen = button.dataset.scenePick;
      const kind = chosen || stage.dataset.auto || 'day';
      applyKind(kind);
      if (label) label.textContent = (conf.labels[kind] || kind) + (chosen ? '' : ' · 自动');
      for (const item of menu.querySelectorAll('[data-scene-pick]')) {
        item.setAttribute('aria-pressed', String(item.dataset.scenePick === chosen));
      }
      try { menu.hidePopover(); } catch (error) { /* 不认 popover 的老浏览器 */ }
      const body = new FormData();
      body.append('csrf', conf.csrf);
      body.append('scene', chosen);
      fetch(conf.url, { method: 'POST', body: body, headers: { 'X-Requested-With': 'fetch' }, credentials: 'same-origin' })
        .then(function (response) { return response.json(); })
        .then(function (result) { if (!result.ok) throw new Error('scene'); })
        .catch(function () {
          if (window.appToast) window.appToast('error', '背景没存上', '这一页上已经换了，下次打开还是原来的。页面可能已经过期，刷新后再试。');
        });
    });
  }

  if (still.matches) return;

  // ---- 风景跟着动
  function puff(m) {
    if (!puffs || puffs.childElementCount > 14) return;
    const dot = document.createElement('i');
    dot.className = 'scene-puff';
    // 脚后面：往右走扬在左脚后，往左走扬在右脚后
    dot.style.left = Math.round(m.dir > 0 ? m.x + 2 : m.x + m.el.offsetWidth - 5) + 'px';
    dot.style.setProperty('--sway', (m.dir > 0 ? -1 : 1) * (4 + Math.random() * 5) + 'px');
    dot.addEventListener('animationend', function () { dot.remove(); });
    puffs.append(dot);
  }
  function leaf(tree) {
    const piece = document.createElement('i');
    piece.className = 'scene-leaf';
    piece.style.left = Math.round(tree.half + (Math.random() - 0.5) * tree.half) + 'px';
    piece.style.setProperty('--sway', (Math.random() < 0.5 ? -1 : 1) * (6 + Math.random() * 10) + 'px');
    piece.addEventListener('animationend', function () { piece.remove(); });
    tree.el.append(piece);
  }
  function rustle(tree) {
    tree.el.classList.remove('is-rustle');
    void tree.el.offsetWidth;
    tree.el.classList.add('is-rustle');
    leaf(tree);
  }

  function scenery(now, dt) {
    if (!scene) return;
    // 镜头：几只站的地方平均一下，慢慢追过去
    const width = stage.clientWidth || 1;
    const centre = crew.reduce(function (sum, m) { return sum + middle(m); }, 0) / crew.length;
    const target = Math.max(-0.5, Math.min(0.5, centre / width - 0.5));
    cam += (target - cam) * Math.min(1, dt * 1.6);
    stage.style.setProperty('--cam', cam.toFixed(4));
    // 踩草：脚底下的弯下去，往走的方向倒
    for (const tuft of tufts) {
      let foot = null;
      for (const m of crew) {
        if (Math.abs(middle(m) - tuft.x) < m.el.offsetWidth * 0.42) { foot = m; break; }
      }
      if (!!foot !== tuft.pressed) {
        tuft.pressed = !!foot;
        tuft.el.classList.toggle('is-pressed', tuft.pressed);
      }
      if (foot) tuft.el.style.setProperty('--lean', String(foot.dir));
    }
    // 树：刚走到树底下的那一刻晃一晃、掉片叶子（晃完歇一会儿）
    for (const tree of trees) {
      const centreX = treeCentre(tree);
      const under = crew.filter(function (m) { return Math.abs(middle(m) - centreX) < tree.half; });
      const arrived = under.some(function (m) { return tree.under.indexOf(m) < 0 && m.mode === 'walk'; });
      if (arrived && now >= tree.calmUntil) {
        tree.calmUntil = now + RUSTLE_GAP;
        rustle(tree);
      }
      tree.under = under;
    }
    // 扬灰
    for (const m of crew) {
      if (m.mode === 'walk' && !m.held && now - m.puffAt > PUFF_EVERY) {
        m.puffAt = now;
        puff(m);
      }
    }
  }

  // ---- 走、跳、站：一帧一帧推着走（睡着的那只不动；雪地上走着走着会滑一跤）
  let last = performance.now();
  for (const m of crew) if (!m.sleeping) setMode(m, 'rest', last - Math.random() * 1500);   // 错开出发
  function frame(now) {
    const dt = Math.min(0.05, (now - last) / 1000);   // 页面从后台回来时别一下子瞬移
    last = now;
    const snowy = stage.dataset.kind === 'snow';
    for (const m of crew) {
      if (m.held || m.sleeping || m.chatting) continue;
      if (m.mode === 'walk') {
        if (snowy && !m.slipping && Math.random() < dt * SLIP_RATE) {
          m.slipping = true;
          m.el.classList.add('is-slip');
        }
        const step = m.speed * zoom * dt * m.dir;
        const arrived = m.dir > 0 ? m.x + step >= m.target : m.x + step <= m.target;
        m.x = Math.min(Math.max(arrived ? m.target : m.x + step, 0), room(m));
        put(m);
        if (arrived) next(m, now);
      } else if (now >= m.until) {
        next(m, now);
      }
    }
    scenery(now, dt);
    window.requestAnimationFrame(frame);
  }
  window.requestAnimationFrame(frame);
  // 窗口变了：宽屏、窄屏换了就重新读放大多少；站到台子外面去的拉回来，草和树重新量，雨雪按新的宽窄重撒
  let resizeTimer = null;
  window.addEventListener('resize', function () {
    readScale();
    for (const m of crew) {
      m.x = Math.min(m.x, room(m));
      m.target = Math.min(m.target, room(m));
      put(m);
    }
    measure();
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { weather(stage.dataset.kind); }, 300);
  });
})();
