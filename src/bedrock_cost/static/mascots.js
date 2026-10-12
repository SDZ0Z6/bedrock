/* 概览顶上的像素小家伙和它们的小风景（templates/_mascot.html、welcome.py）。
 *
 * 小家伙：在台子上走来走去、停一会儿、跳一会儿舞，随机安排。鼠标移上去（或者用键盘移到它身上）它就停下来举手，
 * 头上冒个对话框说一句；点一下蹦一下、换一句。说什么从 #mascot-talk 里挑：一多半说自己脾气那一类（data-mood），
 * 有时候说看场合的（叫名字、看几点、看概览上的数），有时候说大家都会说的；被点了多半是「哎哟」那一类。
 * 最近说过的十句先不说，免得几只轮着说同一句。
 *
 * 风景跟着它们动：
 *   · 视差：按几只小家伙站的地方算一个「镜头」（--cam，-0.5 ~ 0.5，慢慢追过去），天、远山、树三层各按自己的
 *     深度挪（style.css）——越远的挪得越多，草地不动（小家伙站在上面）；
 *   · 踩草：小家伙脚底下的草和花弯下去，往它走的方向倒，走开就弹回来；
 *   · 树：有小家伙走到树底下，树冠晃一晃、掉一片叶子；
 *   · 扬灰：走路时脚后面扬一点小灰。
 * 系统设了「减少动态效果」：都站着不动，风景也不动（CSS 也把动画关了），对话框照样有。页面放到后台时 rAF 自己会停。 */
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
  const rand = function (range) { return range[0] + Math.random() * (range[1] - range[0]); };

  const crew = Array.prototype.map.call(stage.querySelectorAll('[data-mascot]'), function (el) {
    return { el: el, bubble: el.querySelector('[data-bubble]'), mood: el.dataset.mood, x: 0, target: 0, dir: 1,
             speed: rand(SPEED), mode: 'rest', until: 0, held: false, hideTimer: null, puffAt: 0 };
  });
  if (!crew.length) return;

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
  // 走去哪：随便挑几个地方，选离别的小家伙（站着的地方、正要去的地方）最远的那个，免得几只挤成一团；
  // 至少挪开一段，不然看着像原地抽筋
  function destination(m) {
    const width = room(m);
    const others = [];
    for (const other of crew) {
      if (other === m) continue;
      others.push(other.x);
      if (other.mode === 'walk') others.push(other.target);
    }
    let best = m.x, score = -1;
    for (let tries = 0; tries < 5; tries++) {
      let spot = Math.random() * width;
      if (Math.abs(spot - m.x) < 40) spot = m.x < width / 2 ? Math.min(width, m.x + 90) : Math.max(0, m.x - 90);
      const gap = others.length ? Math.min.apply(null, others.map(function (x) { return Math.abs(x - spot); })) : width;
      if (gap > score) { best = spot; score = gap; }
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
    if (poked && Math.random() < 0.6) return pick(talk.poked);
    const roll = Math.random();
    let line = '';
    if (roll < 0.25) line = pick(talk.context);
    else if (roll < 0.7) line = pick(talk.moods[m.mood]);
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
    m.el.classList.remove('is-walking', 'is-dancing');
    m.el.classList.add('is-talking');
    say(m, false);
  }
  function release(m) {
    if (!m.held) return;
    m.held = false;
    m.el.classList.remove('is-talking');
    clearTimeout(m.hideTimer);
    m.hideTimer = setTimeout(function () { m.bubble.hidden = true; }, 900);
    setMode(m, 'rest', performance.now());
  }
  for (const m of crew) {
    m.el.addEventListener('mouseenter', function () { hold(m); });
    m.el.addEventListener('mouseleave', function () { release(m); });
    m.el.addEventListener('focus', function () { hold(m); });
    m.el.addEventListener('blur', function () { release(m); });
    m.el.addEventListener('click', function () {
      if (m.held) say(m, true); else hold(m);
      if (still.matches) return;
      m.el.classList.remove('is-hop');
      void m.el.offsetWidth;   // 去掉再加上，动画才会重新放一遍
      m.el.classList.add('is-hop');
    });
    m.el.addEventListener('animationend', function (event) {
      if (event.animationName === 'mascot-hop') m.el.classList.remove('is-hop');
    });
  }

  if (still.matches) return;

  // ---- 风景：草和树在台子上的位置（窗口变了重新量）
  const scene = stage.querySelector('[data-scene]');
  const puffs = scene ? scene.querySelector('[data-puffs]') : null;
  const mid = scene ? scene.querySelector('[data-layer="mid"]') : null;
  const tufts = scene ? Array.prototype.map.call(scene.querySelectorAll('[data-tuft]'), function (el) {
    return { el: el, x: 0, pressed: false };
  }) : [];
  const trees = scene ? Array.prototype.map.call(scene.querySelectorAll('[data-tree]'), function (el) {
    return { el: el, x: 0, half: 0, under: [], calmUntil: 0 };
  }) : [];
  const RUSTLE_GAP = 1200;    // 一棵树晃完至少歇这么久：站在树边上的小家伙一进一出，别让它一直掉叶子
  const MID_DEPTH = 0.4, PAN = 56;    // 和 style.css 里 .scene-mid 的深度、--pan 一样
  function measure() {
    for (const tuft of tufts) tuft.x = tuft.el.offsetLeft + tuft.el.offsetWidth / 2;
    for (const tree of trees) {
      tree.x = (mid ? mid.offsetLeft : 0) + tree.el.offsetLeft + tree.el.offsetWidth / 2;
      tree.half = tree.el.offsetWidth / 2;
    }
  }
  measure();
  let cam = 0;

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
    const top = tree.el.querySelector('.tree-top');
    if (!top) return;
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
    const shift = cam * PAN * MID_DEPTH;
    for (const tree of trees) {
      const centreX = tree.x + shift;
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

  // ---- 走、跳、站：一帧一帧推着走
  let last = performance.now();
  for (const m of crew) setMode(m, 'rest', last - Math.random() * 1500);   // 错开出发
  function frame(now) {
    const dt = Math.min(0.05, (now - last) / 1000);   // 页面从后台回来时别一下子瞬移
    last = now;
    for (const m of crew) {
      if (m.held) continue;
      if (m.mode === 'walk') {
        const step = m.speed * dt * m.dir;
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
  // 窗口变了：站到台子外面去的拉回来，草和树重新量
  window.addEventListener('resize', function () {
    for (const m of crew) {
      m.x = Math.min(m.x, room(m));
      m.target = Math.min(m.target, room(m));
      put(m);
    }
    measure();
  });
})();
