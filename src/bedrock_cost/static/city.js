/* 概览顶上的小城：city.py 摆好（#city-data），这里用 three.js 画，方块体素风，斜 45° 看过去。
 *
 * 一座正方形的城：棋盘格的马路（中间虚线，路口斑马线），街区边上一圈人行道；一条河从北往南穿过城，马路过河架桥。
 * 公司楼一个客户一栋（楼顶挂头像和名字）；仓库是名下的账号，散在各个街区里，屋顶和它的公司楼同一个颜色，屋顶的
 * 牌子写邮箱 @ 前面那段；结算了的账号是一块小公园；库存在东北角的仓储区。天上几片云慢慢飘，地上有影子。
 * 动的是搬箱子的小家伙：最近一小时有调用的仓库才有（门开着），举着箱子走到它的公司楼，进去放下，空着手走回来，
 * 进仓库再搬一箱；几只错开着走。路是每一趟在格子地图上找的：只走人行道、斑马线、桥、门口的砖地，有时候先绕到
 * 随便哪个路口再过去（走的街不一样），偶尔停下来左右张望。
 *
 * 鼠标：放到公司楼、仓库、小公园、小家伙上弹提示（和图表的提示框一个样子）；点一下镜头飞过去拉近看特写，别的地方
 * 淡下去，右边一张卡片（仓库：余额、使用率、近 14 天调用……「进账号页」；公司楼：客户的数、「进客户页」）；
 * 点小家伙镜头跟着它走。点空白处、按 Esc、点 ✕ 飞回去。左键拖着绕城转，右键拖着平移；滚轮朝鼠标那一点放大缩小
 * （缩回整个城以后再滚就是滚页面），+ / − 也能放大缩小，⟲ 回到原位。手机上点一下就是特写。小家伙偶尔说一句
 * （#mascot-talk）。页面在后台、欢迎条滚出屏幕时不画；系统设了「减少动态效果」小家伙站在门口不动、云不飘、
 * 镜头不飞（直接到那儿）。
 *
 * 没有 WebGL2、three.js 加载不出来、画的时候出错：换回原来的像素风景（欢迎条上去掉 is-city，发 city:off，
 * mascots.js 接着跑）。three.js 不小（static/vendor/three，七百多 KB），确定要画了才 import。 */
const welcome = document.querySelector('.welcome');
const host = document.querySelector('[data-city]');
const source = document.getElementById('city-data');
const canvas = host ? host.querySelector('canvas') : null;
let context = null;
if (welcome && canvas && source) {
  try {
    context = canvas.getContext('webgl2', { antialias: true, alpha: true, powerPreference: 'low-power' });
  } catch (error) {
    context = null;
  }
}
if (context) {
  // 先换上（mascots.js 在后面跑，看到 is-city 就不动原来的风景），three.js 再慢慢来
  welcome.classList.add('is-city');
  host.hidden = false;
  start().catch(fail);
}

function fail(error) {
  console.warn('小城画不出来，换回像素风景', error);
  welcome.classList.remove('is-city');
  host.hidden = true;
  document.dispatchEvent(new CustomEvent('city:off'));
}

async function start() {
  const THREE = await import('./vendor/three/three.module.min.js');
  const data = JSON.parse(source.textContent);
  if (document.fonts && document.fonts.ready) await document.fonts.ready;
  city(THREE, data);
}

// ------------------------------------------------------------------ 颜色、高度
const P = {
  asphalt: '#7a7772', dash: '#efeadf', zebra: '#f2eee5', soil: '#a3855f', bed: '#5d8aa6', water: '#82bcdc',
  walk: '#ddd5c5', grass: '#a6c486', grass2: '#98b97a', plaza: '#e6ddca', yard: '#d6d1c6',
  rail: '#efe7d8', pillar: '#b9b2a4', cloud: '#ffffff',
  wall: '#f1e9d9', facade: '#ede6d7', facadeTop: '#d8d0bf', window: '#8db3cc', door: '#3d4045',
  shutter: '#9da2a8', shutterLine: '#868b92', post: '#8d8a84',
  trunk: '#8e6b4e', leaf: ['#7fae62', '#8cbb6c', '#6f9f58'], pine: ['#5f8f5a', '#6d9c63'],
  stone: '#d8d1c3', stoneDark: '#bdb5a6', bench: '#b0835b', benchLeg: '#6e6a64',
  flower: ['#e8836f', '#f2c45c', '#c993d8', '#f4a7b9', '#ffffff'], crate: '#c49a68', crateDark: '#a77f52',
  mascot: '#d97757', eye: '#141413', box: '#d9b27c', tape: '#b98a52',
  // 跟着背景换的：樱花、秋天的树，雪，路灯，夜里亮着的窗户，小家伙的伞和围巾
  sakura: ['#f4b3c6', '#f0a1b7', '#f8cad8'], autumn: ['#d9822b', '#c9572f', '#e0b23e', '#b8663a'], snow: '#f6f8fb',
  lamp: '#5f5c57', bulb: '#efe3c0', glow: ['#ffd27f', '#ffe1a0', '#ffc66b', '#fff0c8'], signal: '#34373b',
  lit: ['#ff4b3e', '#ffc53a', '#3ddc6a'],
  umbrella: ['#4f86c6', '#d0453a', '#e2a93b'], scarf: '#c8453b',
};
const TONES = { ok: '#2c7652', warn: '#a87412', danger: '#bc3b2e', none: '#87867f' };
const WALK_TOP = 0.1, GRASS_TOP = 0.12, PAVE_TOP = 0.125;
const ELEVATION = 30 * Math.PI / 180;     // 镜头往下看多少度
const YAW = Math.PI / 4;                  // 默认从东南边斜 45° 看过去：正方形的城看起来是个菱形
const SPEED = 0.9;                        // 小家伙走路，一秒几格（再乘仓库的 speed）
const FOCUS_ZOOM = 2.6;                   // 点开特写放大到几倍
const FOCUS_TILT = 44 * Math.PI / 180;    // 特写时往下看得陡一点，前面高的楼少挡一点
const FONT = getComputedStyle(document.body).fontFamily || 'sans-serif';
const still = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
const touchy = window.matchMedia ? window.matchMedia('(hover: none)') : { matches: false };

function rng(seed) {
  let a = seed >>> 0;
  return function () {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function city(THREE, data) {
  const W = data.size[0], D = data.size[1];
  const X = function (x) { return x - W / 2; };
  const Z = function (z) { return z - D / 2; };
  const random = rng(Math.round(W * 131 + D * 17));
  const color = new THREE.Color();

  // ---------------------------------------------------------------- 一堆方块拼成一个网格
  // 每个方块一种颜色（稍微抖一点，看着不那么死板），底面不画（看不见）
  function blocks() {
    const pos = [], nor = [], col = [], idx = [];
    function add(x0, y0, z0, x1, y1, z1, hex, jitter) {
      color.set(hex);
      if (jitter !== 0) color.multiplyScalar(1 + (random() - 0.5) * (jitter === undefined ? 0.06 : jitter));
      const ax = X(Math.min(x0, x1)), bx = X(Math.max(x0, x1));
      const ay = Math.min(y0, y1), by = Math.max(y0, y1);
      const az = Z(Math.min(z0, z1)), bz = Z(Math.max(z0, z1));
      const faces = [
        [1, 0, 0, bx, ay, bz, bx, ay, az, bx, by, az, bx, by, bz],
        [-1, 0, 0, ax, ay, az, ax, ay, bz, ax, by, bz, ax, by, az],
        [0, 1, 0, ax, by, bz, bx, by, bz, bx, by, az, ax, by, az],
        [0, 0, 1, ax, ay, bz, bx, ay, bz, bx, by, bz, ax, by, bz],
        [0, 0, -1, bx, ay, az, ax, ay, az, ax, by, az, bx, by, az],
      ];
      for (const f of faces) {
        const base = pos.length / 3;
        for (let i = 0; i < 4; i++) {
          pos.push(f[3 + i * 3], f[4 + i * 3], f[5 + i * 3]);
          nor.push(f[0], f[1], f[2]);
          col.push(color.r, color.g, color.b);
        }
        idx.push(base, base + 1, base + 2, base, base + 2, base + 3);
      }
    }
    function mesh(material) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
      geometry.setAttribute('normal', new THREE.Float32BufferAttribute(nor, 3));
      geometry.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
      geometry.setIndex(idx);
      geometry.computeBoundingSphere();
      return new THREE.Mesh(geometry, material);
    }
    return { add: add, mesh: mesh };
  }

  const lambert = new THREE.MeshLambertMaterial({ vertexColors: true });
  const glassy = new THREE.MeshLambertMaterial({ vertexColors: true, transparent: true, opacity: 0.9 });
  const ground = blocks(), solid = blocks(), water = blocks();
  // 夜里会亮的（一多半窗户、路灯的灯泡），下雪才露出来的那层雪，三套树冠（平时绿的，樱花粉的，秋天黄的红的）
  const lights = blocks(), snowy = blocks();
  const crowns = { day: blocks(), sakura: blocks(), autumn: blocks() };
  // 哪扇窗夜里亮、樱花秋天的树挑哪个颜色：和摆城的随机数分开，城还是原来那个样子
  const luck = rng(Math.round(W * 7 + D * 3));
  function pane(x0, y0, z0, x1, y1, z1) {
    (luck() < 0.62 ? lights : solid).add(x0, y0, z0, x1, y1, z1, P.window, 0.08);
  }
  const bulbs = [];            // 红绿灯的灯泡：哪个路口、管哪条路（ns 南北 / ew 东西）、第几个（0 红 1 黄 2 绿）
  const scene = new THREE.Scene();
  const picks = [];            // 悬浮、点开的：看不见的盒子，userData 是 {kind, tip, card, url, sign, at, face}
  const hull = [];             // 城的外轮廓上的点（底座的角、楼顶连牌子）：镜头按它们框住整个城
  const outline = function (x0, z0, x1, z1, y) {
    hull.push([x0, y, z0], [x1, y, z0], [x0, y, z1], [x1, y, z1]);
  };

  function pick(x0, y0, z0, x1, y1, z1, item) {
    const box = new THREE.Mesh(new THREE.BoxGeometry(Math.abs(x1 - x0), Math.abs(y1 - y0), Math.abs(z1 - z0)));
    box.position.set(X((x0 + x1) / 2), (y0 + y1) / 2, Z((z0 + z1) / 2));
    box.updateMatrixWorld(true);
    item.at = [(x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2];     // 特写时镜头对着这儿（城里的坐标）
    box.userData = item;
    picks.push(box);
  }

  // ---------------------------------------------------------------- 地面
  // 底座：上面一层沥青（马路），侧面是土；河从中间挖开。街区凸起来一点：边上一圈人行道，中间草地（仓储区是水泥地）。
  // 马路中间画虚线（路口空着），路口四边是斑马线；东西向的马路过河架桥，桥两边是人行道和栏杆
  const rects = data.ground;
  const of = function (kind) { return rects.filter(function (r) { return r.k === kind; }); };
  const rivers = of('water'), bridges = of('bridge'), roads = of('road'), crossings = of('cross');
  for (const base of of('base')) {
    let cuts = [base.x];
    for (const w of rivers) cuts.push(w.x, w.x + w.w);
    cuts.push(base.x + base.w);
    outline(base.x, base.z, base.x + base.w, base.z + base.d, -1.2);
    outline(base.x, base.z, base.x + base.w, base.z + base.d, 0.4);
    for (let i = 0; i + 1 < cuts.length; i += 2) {
      ground.add(cuts[i], -1.2, base.z, cuts[i + 1], -0.05, base.z + base.d, P.soil, 0);
      ground.add(cuts[i], -0.05, base.z, cuts[i + 1], 0, base.z + base.d, P.asphalt, 0);
    }
  }
  for (const w of rivers) {
    ground.add(w.x, -1.2, w.z, w.x + w.w, -0.75, w.z + w.d, P.bed, 0);
    water.add(w.x, -0.75, w.z, w.x + w.w, -0.24, w.z + w.d, P.water, 0);
  }
  for (const r of of('walk')) ground.add(r.x, 0, r.z, r.x + r.w, WALK_TOP, r.z + r.d, P.walk, 0.02);
  for (const r of of('lawn')) ground.add(r.x, 0, r.z, r.x + r.w, GRASS_TOP, r.z + r.d, P.grass, 0.03);
  for (const r of of('bank')) ground.add(r.x, 0, r.z, r.x + r.w, GRASS_TOP, r.z + r.d, P.grass2, 0.03);
  for (const r of of('yard')) ground.add(r.x, 0, r.z, r.x + r.w, GRASS_TOP, r.z + r.d, P.yard, 0.02);
  for (const r of of('lawn').concat(of('bank'), of('yard'))) {
    snowy.add(r.x, GRASS_TOP, r.z, r.x + r.w, GRASS_TOP + 0.025, r.z + r.d, P.snow, 0);
  }
  for (const r of of('plaza')) ground.add(r.x, 0, r.z, r.x + r.w, PAVE_TOP, r.z + r.d, P.plaza, 0.02);
  for (const b of bridges) {
    ground.add(b.x, -0.12, b.z, b.x + b.w, 0, b.z + b.d, P.asphalt, 0);
    for (const z0 of [b.z, b.z + b.d - 0.6]) ground.add(b.x, 0, z0, b.x + b.w, WALK_TOP, z0 + 0.6, P.walk, 0);
    solid.add(b.x, WALK_TOP, b.z, b.x + b.w, 0.4, b.z + 0.1, P.rail, 0);
    solid.add(b.x, WALK_TOP, b.z + b.d - 0.1, b.x + b.w, 0.4, b.z + b.d, P.rail, 0);
    for (const px of [b.x + b.w * 0.33, b.x + b.w * 0.67]) {
      solid.add(px - 0.18, -0.75, b.z + 0.4, px + 0.18, -0.12, b.z + b.d - 0.4, P.pillar, 0);
    }
  }
  // 马路中间的虚线：路口（和另一个方向的路重叠的地方）空着
  const crosses = function (r, a, b) {
    return roads.some(function (o) {
      if (o.dir === r.dir) return false;
      return r.dir === 'ew' ? (a < o.x + o.w && b > o.x) : (a < o.z + o.d && b > o.z);
    });
  };
  for (const r of roads.concat(bridges.map(function (b) { return Object.assign({ dir: 'ew' }, b); }))) {
    if (r.dir === 'ew') {
      const zc = r.z + r.d / 2;
      for (let x = r.x + 0.3; x + 0.55 < r.x + r.w; x += 1.1) {
        if (!crosses(r, x - 0.2, x + 0.75)) ground.add(x, 0, zc - 0.04, x + 0.55, 0.012, zc + 0.04, P.dash, 0);
      }
    } else {
      const xc = r.x + r.w / 2;
      for (let z = r.z + 0.3; z + 0.55 < r.z + r.d; z += 1.1) {
        if (!crosses(r, z - 0.2, z + 0.75)) ground.add(xc - 0.04, 0, z, xc + 0.04, 0.012, z + 0.55, P.dash, 0);
      }
    }
  }
  // 斑马线：一条条白道顺着马路的方向，人横着走过去（dir 是人走的方向）
  for (const c of crossings) {
    if (c.dir === 'x') {
      for (let x = c.x + 0.12; x + 0.2 <= c.x + c.w - 0.1; x += 0.38) ground.add(x, 0, c.z, x + 0.2, 0.014, c.z + c.d, P.zebra, 0);
    } else {
      for (let z = c.z + 0.12; z + 0.2 <= c.z + c.d - 0.1; z += 0.38) ground.add(c.x, 0, z, c.x + c.w, 0.014, z + 0.2, P.zebra, 0);
    }
  }

  // ---------------------------------------------------------------- 树
  function tree(x, z, size) {
    const s = size, top = GRASS_TOP;
    // 树冠一层一层，三套颜色各画一份（换背景只换露出来的那一套）；每层顶上再铺一层雪，下雪才露出来
    const crown = function (x0, y0, z0, x1, y1, z1, green, pink, fall) {
      crowns.day.add(x0, y0, z0, x1, y1, z1, green);
      crowns.sakura.add(x0, y0, z0, x1, y1, z1, pink, 0);
      crowns.autumn.add(x0, y0, z0, x1, y1, z1, fall, 0);
      snowy.add(x0 + 0.02, y1, z0 + 0.02, x1 - 0.02, y1 + 0.05, z1 - 0.02, P.snow, 0);
    };
    const tone = function (hex) { return '#' + color.set(hex).multiplyScalar(0.97 + luck() * 0.06).getHexString(); };
    if (random() < 0.3) {
      // 松树：一层比一层小，一年到头都是绿的
      solid.add(x - 0.07 * s, top, z - 0.07 * s, x + 0.07 * s, top + 0.35 * s, z + 0.07 * s, P.trunk);
      const green = P.pine[Math.floor(random() * P.pine.length)];
      let y = top + 0.3 * s, half = 0.42 * s;
      for (let i = 0; i < 4; i++) {
        const same = tone(green);
        crown(x - half, y, z - half, x + half, y + 0.3 * s, z + half, green, same, same);
        y += 0.26 * s;
        half *= 0.72;
      }
      return;
    }
    solid.add(x - 0.08 * s, top, z - 0.08 * s, x + 0.08 * s, top + 0.5 * s, z + 0.08 * s, P.trunk);
    const green = P.leaf[Math.floor(random() * P.leaf.length)];
    const pink = P.sakura[Math.floor(luck() * P.sakura.length)], fall = P.autumn[Math.floor(luck() * P.autumn.length)];
    const y = top + 0.45 * s;
    crown(x - 0.45 * s, y, z - 0.45 * s, x + 0.45 * s, y + 0.5 * s, z + 0.45 * s, green, tone(pink), tone(fall));
    crown(x - 0.32 * s, y + 0.5 * s, z - 0.32 * s, x + 0.32 * s, y + 0.8 * s, z + 0.32 * s, green, tone(pink), tone(fall));
    crown(x - 0.15 * s, y + 0.8 * s, z - 0.15 * s, x + 0.15 * s, y + 0.95 * s, z + 0.15 * s, green, tone(pink), tone(fall));
  }
  for (const t of data.trees) tree(t[0], t[1], t[2] * 1.6);

  // ---------------------------------------------------------------- 牌子（贴图的小板子，一直朝着镜头）
  // 牌子在屏幕上一直一样大（height 是像素）：缩小看全城也认得出字，放大了也不会大得挡住房子。aim() 换镜头时重新量
  const boards = [];
  function plate(text, bg, fg, height) {
    const k = 4, fontSize = 15 * k;
    const sheet = document.createElement('canvas');
    const pen = sheet.getContext('2d');
    pen.font = '600 ' + fontSize + 'px ' + FONT;
    const w = Math.ceil(pen.measureText(text).width + 18 * k), h = 26 * k;
    sheet.width = w;
    sheet.height = h + 5 * k;
    pen.font = '600 ' + fontSize + 'px ' + FONT;
    pen.fillStyle = bg;
    pen.beginPath();
    if (pen.roundRect) pen.roundRect(0, 0, w, h, 7 * k); else pen.rect(0, 0, w, h);
    pen.fill();
    pen.beginPath();
    pen.moveTo(w / 2 - 5 * k, h);
    pen.lineTo(w / 2, h + 5 * k);
    pen.lineTo(w / 2 + 5 * k, h);
    pen.fill();
    pen.fillStyle = fg;
    pen.textAlign = 'center';
    pen.textBaseline = 'middle';
    pen.fillText(text, w / 2, h / 2 + k);
    return sprite(sheet, height);
  }
  function sprite(sheet, height) {
    const texture = new THREE.CanvasTexture(sheet);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.anisotropy = 4;
    const material = new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false });
    const s = new THREE.Sprite(material);
    s.center.set(0.5, 0);
    s.userData.px = height;
    s.userData.ratio = sheet.width / sheet.height;
    s.renderOrder = 2;
    boards.push(s);
    return s;
  }
  function sizeBoards() {
    // 一个像素在地上是多大：画布宽对着的那一段世界，再除放大的倍数
    const per = (camera.right - camera.left) / camera.zoom / (canvas.clientWidth || 1);
    for (const s of boards) {
      const lit = (hovered && hovered.sign === s) || (focus && focus.item.sign === s);
      const k = lit ? 1.18 : 1;
      s.scale.set(s.userData.px * s.userData.ratio * per * k, s.userData.px * per * k, 1);
    }
  }

  // 客户的头像：插画用图，没有插画是名字的第一个字（底色和页面上的 .cletter 一样，从样式表里读）
  function letterColor(tone) {
    const probe = document.createElement('span');
    probe.className = 'cletter lt-' + tone;
    probe.style.cssText = 'position: absolute; visibility: hidden;';
    document.body.append(probe);
    const bg = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return bg || '#d4896a';
  }
  function billboard(office) {
    const k = 4, fontSize = 15 * k, face = 22 * k;
    const sheet = document.createElement('canvas');
    const pen = sheet.getContext('2d');
    pen.font = '600 ' + fontSize + 'px ' + FONT;
    const w = Math.ceil(face + pen.measureText(office.name).width + 26 * k), h = 32 * k;
    sheet.width = w;
    sheet.height = h;
    const s = sprite(sheet, 30);
    function draw(image) {
      pen.clearRect(0, 0, w, h);
      pen.fillStyle = '#fffdf8';
      pen.strokeStyle = '#e4ddd0';
      pen.lineWidth = 2 * k;
      pen.beginPath();
      if (pen.roundRect) pen.roundRect(k, k, w - 2 * k, h - 2 * k, 9 * k); else pen.rect(k, k, w - 2 * k, h - 2 * k);
      pen.fill();
      pen.stroke();
      const cx = 5 * k + face / 2, cy = h / 2;
      pen.save();
      pen.beginPath();
      pen.arc(cx, cy, face / 2, 0, Math.PI * 2);
      pen.clip();
      if (image) {
        pen.drawImage(image, cx - face / 2, cy - face / 2, face, face);
      } else {
        pen.fillStyle = office.avatar.letter ? letterColor(office.avatar.tone) : '#e8e1d3';
        pen.fillRect(cx - face / 2, cy - face / 2, face, face);
        pen.fillStyle = '#2b2723';
        pen.font = '600 ' + Math.round(fontSize * 0.9) + 'px ' + FONT;
        pen.textAlign = 'center';
        pen.textBaseline = 'middle';
        pen.fillText(office.avatar.letter || '', cx, cy + k);
      }
      pen.restore();
      pen.fillStyle = '#2b2723';
      pen.font = '600 ' + fontSize + 'px ' + FONT;
      pen.textAlign = 'left';
      pen.textBaseline = 'middle';
      pen.fillText(office.name, 5 * k + face + 8 * k, h / 2 + k);
      s.material.map.needsUpdate = true;
    }
    draw(null);
    if (office.avatar.img) {
      const image = new Image();
      image.onload = function () { draw(image); requestRender(); };
      image.src = office.avatar.img;
    }
    return s;
  }

  // ---------------------------------------------------------------- 公司楼
  const FLOOR = 0.9;
  for (const o of data.offices) {
    const x0 = o.x + 0.1, x1 = o.x + o.w - 0.1, z0 = o.z, z1 = o.z + o.d;
    const y0 = GRASS_TOP, y1 = GRASS_TOP + o.h;
    outline(x0, z0, x1, z1, y1 + 1.75);
    solid.add(x0, y0, z0, x1, y1, z1, P.facade, 0.03);
    // 每层一排窗户，四面都有；最底下一层南面是门
    const floors = Math.max(2, Math.floor((o.h - 0.3) / FLOOR));
    for (let f = 0; f < floors; f++) {
      const wy = y0 + f * FLOOR + 0.3;
      if (wy + 0.42 > y1 - 0.15) break;
      for (let x = x0 + 0.3; x + 0.34 <= x1 - 0.25; x += 0.62) {
        if (f === 0 && Math.abs(x + 0.17 - o.door[0]) < 0.75) continue;
        pane(x, wy, z1, x + 0.34, wy + 0.42, z1 + 0.04);
        pane(x, wy, z0 - 0.04, x + 0.34, wy + 0.42, z0);
      }
      for (let z = z0 + 0.28; z + 0.34 <= z1 - 0.25; z += 0.62) {
        pane(x1, wy, z, x1 + 0.04, wy + 0.42, z + 0.34);
        pane(x0 - 0.04, wy, z, x0, wy + 0.42, z + 0.34);
      }
    }
    const dx = o.door[0];
    solid.add(dx - 0.42, y0, z1, dx + 0.42, y0 + 0.68, z1 + 0.05, P.door, 0);
    solid.add(dx - 0.62, y0 + 0.72, z1, dx + 0.62, y0 + 0.8, z1 + 0.36, o.roof, 0);
    // 楼顶：一圈檐、一个小机房，上面挂牌子
    solid.add(x0 - 0.06, y1, z0 - 0.06, x1 + 0.06, y1 + 0.12, z1 + 0.06, o.roof, 0);
    snowy.add(x0 - 0.04, y1 + 0.12, z0 - 0.04, x1 + 0.04, y1 + 0.15, z1 + 0.04, P.snow, 0);
    const mx = (x0 + x1) / 2, mz = (z0 + z1) / 2;
    solid.add(mx - 0.7, y1 + 0.12, mz - 0.5, mx + 0.7, y1 + 0.5, mz + 0.5, P.facadeTop, 0);
    snowy.add(mx - 0.68, y1 + 0.5, mz - 0.48, mx + 0.68, y1 + 0.53, mz + 0.48, P.snow, 0);
    const board = billboard(o);
    board.userData.owner = o.id;
    board.position.set(X(mx), y1 + 0.58, Z(mz));
    scene.add(board);
    pick(x0, y0, z0, x1, y1 + 1.2, z1, { kind: 'office', id: o.id, tip: o.tip, card: o.card, url: o.url, sign: board,
                                         face: 's', height: o.h });
  }

  // ---------------------------------------------------------------- 仓库
  for (const h of data.houses) {
    const x0 = h.x, x1 = h.x + h.w, z0 = h.z, z1 = h.z + h.d;
    const height = h.size[2], y0 = GRASS_TOP, y1 = GRASS_TOP + height;
    solid.add(x0, y0, z0, x1, y1, z1, P.wall, 0.03);
    // 屋顶：三层台阶的人字顶，屋脊垂直于有门的那一面
    const across = h.face === 's' || h.face === 'n';        // 门开在南北两面
    const span = across ? h.w : h.d;
    let y = y1;
    for (let i = 0; i < 3; i++) {
      const half = span / 2 * (1 - 0.3 * i) + (i === 0 ? 0.1 : 0);
      if (across) {
        const mx = (x0 + x1) / 2;
        solid.add(mx - half, y, z0 - 0.1, mx + half, y + 0.14, z1 + 0.1, h.roof, 0.04);
        snowy.add(mx - half + 0.03, y + 0.14, z0 - 0.08, mx + half - 0.03, y + 0.17, z1 + 0.08, P.snow, 0);
      } else {
        const mz = (z0 + z1) / 2;
        solid.add(x0 - 0.1, y, mz - half, x1 + 0.1, y + 0.14, mz + half, h.roof, 0.04);
        snowy.add(x0 - 0.08, y + 0.14, mz - half + 0.03, x1 + 0.08, y + 0.17, mz + half - 0.03, P.snow, 0);
      }
      y += 0.14;
    }
    // 门：在用的开着（里面黑的），别的拉着卷帘门（上面三道横线）
    const busy = h.workers > 0;
    const dh = Math.min(0.75, height * 0.62);
    const fx = h.door[0], fz = h.door[1];
    const lo = (across ? fx : fz) - 0.38, hi = (across ? fx : fz) + 0.38;
    const face = { e: [x1, x1 + 0.03], w: [x0 - 0.03, x0], s: [z1, z1 + 0.03], n: [z0 - 0.03, z0] }[h.face];
    const out = { e: [x1 + 0.03, x1 + 0.045], w: [x0 - 0.045, x0 - 0.03], s: [z1 + 0.03, z1 + 0.045], n: [z0 - 0.045, z0 - 0.03] }[h.face];
    if (across) solid.add(lo, y0, face[0], hi, y0 + dh, face[1], busy ? P.door : P.shutter, 0);
    else solid.add(face[0], y0, lo, face[1], y0 + dh, hi, busy ? P.door : P.shutter, 0);
    if (!busy) {
      for (let k = 1; k <= 3; k++) {
        const ly = y0 + dh * k / 4;
        if (across) solid.add(lo, ly, out[0], hi, ly + 0.03, out[1], P.shutterLine, 0);
        else solid.add(out[0], ly, lo, out[1], ly + 0.03, hi, P.shutterLine, 0);
      }
    }
    // 两侧各两个小窗
    if (across) {
      for (const wz of [z0 + h.d * 0.3, z0 + h.d * 0.7]) {
        pane(x1, y1 - 0.42, wz - 0.14, x1 + 0.03, y1 - 0.2, wz + 0.14);
        pane(x0 - 0.03, y1 - 0.42, wz - 0.14, x0, y1 - 0.2, wz + 0.14);
      }
    } else {
      for (const wx of [x0 + h.w * 0.3, x0 + h.w * 0.7]) {
        pane(wx - 0.14, y1 - 0.42, z1, wx + 0.14, y1 - 0.2, z1 + 0.03);
        pane(wx - 0.14, y1 - 0.42, z0 - 0.03, wx + 0.14, y1 - 0.2, z0);
      }
    }
    // 门这一头的屋脊上立一块牌子：门朝哪边、镜头转到哪儿都看得见
    const top = y1 + 0.42;
    const at = { e: [x1 - 0.32, fz], w: [x0 + 0.32, fz], s: [fx, z1 - 0.32], n: [fx, z0 + 0.32] }[h.face];
    for (const k of [-0.16, 0.16]) {
      const px = across ? at[0] + k : at[0], pz = across ? at[1] : at[1] + k;
      solid.add(px - 0.025, top - 0.02, pz - 0.025, px + 0.025, top + 0.14, pz + 0.025, P.post, 0);
    }
    const sign = plate(h.sign.text, TONES[h.sign.tone] || TONES.none, '#ffffff', 19);
    sign.userData.owner = h.office || '';
    sign.position.set(X(at[0]), top + 0.1, Z(at[1]));
    scene.add(sign);
    outline(x0, z0, x1, z1, top + 0.7);
    pick(x0, y0, z0, x1, top + 0.66, z1, { kind: 'house', id: h.id, tip: h.tip, card: h.card, url: h.url, sign: sign,
                                           face: h.face, height: height });
  }

  // ---------------------------------------------------------------- 结算了的账号：一块小公园
  for (const p of data.parks) {
    const x0 = p.x, x1 = p.x + p.w, z0 = p.z, z1 = p.z + p.d;
    ground.add(x0, GRASS_TOP - 0.01, z0, x1, GRASS_TOP + 0.03, z1, P.grass2, 0.02);
    const mx = (x0 + x1) / 2, mz = (z0 + z1) / 2;
    ground.add(mx - 0.22, GRASS_TOP + 0.03, z0 + 0.15, mx + 0.22, GRASS_TOP + 0.045, z1 - 0.15, P.plaza, 0.02);
    tree(x0 + p.w * 0.27, z0 + p.d * 0.3, 0.9);
    bench(mx + 0.55, mz + 0.2, 3);
    flowers(x0 + p.w * 0.25, z0 + p.d * 0.72);
    solid.add(mx - 0.16, GRASS_TOP, z1 - 0.42, mx + 0.16, GRASS_TOP + 0.24, z1 - 0.3, P.stoneDark, 0);
    pick(x0, GRASS_TOP, z0, x1, GRASS_TOP + 0.9, z1, { kind: 'park', id: p.id, tip: p.tip, card: p.card, url: p.url,
                                                       sign: null, face: null, height: 0.9 });
  }

  // ---------------------------------------------------------------- 小摆设
  function bench(x, z, turn) {
    // 在 (0, 0) 朝南摆好，再转 turn 个 90°
    const parts = [
      [-0.42, 0.24, -0.14, 0.42, 0.3, 0.14, P.bench],
      [-0.42, 0.3, -0.16, 0.42, 0.56, -0.11, P.bench],
      [-0.36, 0, -0.12, -0.3, 0.24, 0.12, P.benchLeg],
      [0.3, 0, -0.12, 0.36, 0.24, 0.12, P.benchLeg],
    ];
    for (const q of parts) {
      let a = [q[0], q[2]], b = [q[3], q[5]];
      for (let i = 0; i < turn; i++) { a = [-a[1], a[0]]; b = [-b[1], b[0]]; }
      solid.add(x + a[0], GRASS_TOP + q[1], z + a[1], x + b[0], GRASS_TOP + q[4], z + b[1], q[6], 0.03);
    }
  }
  function flowers(x, z) {
    ground.add(x - 0.4, GRASS_TOP, z - 0.25, x + 0.4, GRASS_TOP + 0.06, z + 0.25, P.soil, 0.03);
    for (let i = 0; i < 6; i++) {
      const fx = x - 0.3 + random() * 0.6, fz = z - 0.17 + random() * 0.34;
      const hex = P.flower[Math.floor(random() * P.flower.length)];
      solid.add(fx - 0.05, GRASS_TOP + 0.06, fz - 0.05, fx + 0.05, GRASS_TOP + 0.16, fz + 0.05, hex, 0.04);
    }
  }
  function roof(x0, z0, x1, z1, y, hex) {
    const span = z1 - z0, mz = (z0 + z1) / 2;
    for (let i = 0; i < 3; i++) {
      const half = span / 2 * (1 - 0.3 * i) + (i === 0 ? 0.08 : 0);
      solid.add(x0 - 0.08, y + i * 0.13, mz - half, x1 + 0.08, y + (i + 1) * 0.13, mz + half, hex, 0.04);
      snowy.add(x0 - 0.06, y + (i + 1) * 0.13, mz - half + 0.03, x1 + 0.06, y + (i + 1) * 0.13 + 0.03, mz + half - 0.03, P.snow, 0);
    }
  }
  for (const p of data.props) {
    if (p.k === 'fountain') {
      const r = 1.15, t = 0.26;
      solid.add(p.x - r, PAVE_TOP, p.z - r, p.x + r, PAVE_TOP + 0.34, p.z - r + t, P.stone, 0);
      solid.add(p.x - r, PAVE_TOP, p.z + r - t, p.x + r, PAVE_TOP + 0.34, p.z + r, P.stone, 0);
      solid.add(p.x - r, PAVE_TOP, p.z - r + t, p.x - r + t, PAVE_TOP + 0.34, p.z + r - t, P.stone, 0);
      solid.add(p.x + r - t, PAVE_TOP, p.z - r + t, p.x + r, PAVE_TOP + 0.34, p.z + r - t, P.stone, 0);
      water.add(p.x - r + t, PAVE_TOP, p.z - r + t, p.x + r - t, PAVE_TOP + 0.24, p.z + r - t, P.water, 0);
      solid.add(p.x - 0.14, PAVE_TOP, p.z - 0.14, p.x + 0.14, PAVE_TOP + 0.8, p.z + 0.14, P.stone, 0);
      solid.add(p.x - 0.42, PAVE_TOP + 0.8, p.z - 0.42, p.x + 0.42, PAVE_TOP + 0.9, p.z + 0.42, P.stone, 0);
      water.add(p.x - 0.32, PAVE_TOP + 0.9, p.z - 0.32, p.x + 0.32, PAVE_TOP + 0.97, p.z + 0.32, P.water, 0);
      water.add(p.x - 0.07, PAVE_TOP + 0.97, p.z - 0.07, p.x + 0.07, PAVE_TOP + 1.25, p.z + 0.07, '#cfe8f5', 0);
    } else if (p.k === 'bench') {
      bench(p.x, p.z, p.r);
    } else if (p.k === 'flowers') {
      flowers(p.x, p.z);
    } else if (p.k === 'crates') {
      const stack = [[-0.45, 0, 0], [0.1, 0, 0.1], [-0.2, 0.5, 0.05], [0.55, 0, -0.35]];
      for (const c of stack) {
        const cx = p.x + c[0], cz = p.z + c[2], cy = GRASS_TOP + c[1];
        solid.add(cx - 0.25, cy, cz - 0.25, cx + 0.25, cy + 0.5, cz + 0.25, P.crate, 0.06);
        solid.add(cx - 0.26, cy + 0.2, cz - 0.26, cx + 0.26, cy + 0.27, cz + 0.26, P.crateDark, 0);
      }
    } else if (p.k === 'home') {
      solid.add(p.x, GRASS_TOP, p.z, p.x + p.w, GRASS_TOP + p.h, p.z + p.d, P.wall, 0.04);
      roof(p.x, p.z, p.x + p.w, p.z + p.d, GRASS_TOP + p.h, p.c);
      solid.add(p.x + p.w * 0.4, GRASS_TOP, p.z + p.d, p.x + p.w * 0.6, GRASS_TOP + 0.6, p.z + p.d + 0.03, P.door, 0);
      for (const wx of [p.x + p.w * 0.2, p.x + p.w * 0.8]) {
        pane(wx - 0.15, GRASS_TOP + 0.55, p.z + p.d, wx + 0.15, GRASS_TOP + 0.85, p.z + p.d + 0.03);
      }
    } else if (p.k === 'lamp') {
      // 路灯：一根杆子，顶上一个灯罩，灯泡黄昏、夜里亮（灯泡在 lights 里），地上照出一圈（lampPools）
      solid.add(p.x - 0.04, WALK_TOP, p.z - 0.04, p.x + 0.04, WALK_TOP + 1.45, p.z + 0.04, P.lamp, 0);
      solid.add(p.x - 0.14, WALK_TOP + 1.45, p.z - 0.14, p.x + 0.14, WALK_TOP + 1.53, p.z + 0.14, P.lamp, 0);
      lights.add(p.x - 0.09, WALK_TOP + 1.33, p.z - 0.09, p.x + 0.09, WALK_TOP + 1.45, p.z + 0.09, P.bulb, 0);
    } else if (p.k === 'signal') {
      // 红绿灯：一根杆子，顶上两个灯箱朝着路口——朝东西的那个管东西向的车，朝南北的那个管南北向的；
      // 灯泡另外画（signalMesh），会变
      solid.add(p.x - 0.035, WALK_TOP, p.z - 0.035, p.x + 0.035, WALK_TOP + 1.3, p.z + 0.035, P.lamp, 0);
      const sx = p.corner === 'ne' ? -1 : 1, sz = p.corner === 'ne' ? 1 : -1;
      const y0 = WALK_TOP + 0.9, y1 = WALK_TOP + 1.36;
      solid.add(Math.min(p.x, p.x + sx * 0.14), y0, p.z - 0.075, Math.max(p.x, p.x + sx * 0.14), y1, p.z + 0.075, P.signal, 0);
      solid.add(p.x - 0.075, y0, Math.min(p.z, p.z + sz * 0.14), p.x + 0.075, y1, Math.max(p.z, p.z + sz * 0.14), P.signal, 0);
      for (let k = 0; k < 3; k++) {
        const y = y1 - 0.09 - k * 0.14;
        bulbs.push({ x: p.x + sx * 0.15, y: y, z: p.z, at: p.at, road: 'ew', light: k });
        bulbs.push({ x: p.x, y: y, z: p.z + sz * 0.15, at: p.at, road: 'ns', light: k });
      }
    }
  }
  // 灯泡一直亮着那个颜色（不受光照，夜里也看得清），没轮到的暗着；每个路口南北、东西轮着放行，各个路口错开一点
  const LIT = P.lit.map(function (hex) { return new THREE.Color(hex); });
  const DIM = LIT.map(function (c) { return c.clone().multiplyScalar(0.18); });
  const signalMesh = new THREE.InstancedMesh(new THREE.BoxGeometry(0.1, 0.1, 0.1), new THREE.MeshBasicMaterial(),
                                             Math.max(1, bulbs.length));
  bulbs.forEach(function (b, i) {
    signalMesh.setMatrixAt(i, new THREE.Matrix4().makeTranslation(X(b.x), b.y, Z(b.z)));
    signalMesh.setColorAt(i, DIM[b.light]);
  });
  signalMesh.count = bulbs.length;
  scene.add(signalMesh);
  const CYCLE = 18;            // 一轮：南北绿 7.5 秒、黄 1.5 秒，东西绿 7.5 秒、黄 1.5 秒
  function signalOf(at, time) {
    const t = (time + at * 3.7) % CYCLE;
    return { ns: t < 7.5 ? 2 : t < 9 ? 1 : 0, ew: t < 9 ? 0 : t < 16.5 ? 2 : 1 };
  }
  function paintSignals(time) {
    for (let i = 0; i < bulbs.length; i++) {
      const b = bulbs[i];
      signalMesh.setColorAt(i, (signalOf(b.at, time)[b.road] === b.light ? LIT : DIM)[b.light]);
    }
    if (signalMesh.instanceColor) signalMesh.instanceColor.needsUpdate = true;
  }
  // 过马路：斑马线横过南北向的马路（dir x），要等南北向的车是红灯、东西向是绿灯才走；横过东西向的反过来
  function mayCross(at, dir, time) {
    const light = signalOf(at, time);
    return dir === 1 ? light.ns === 0 && light.ew === 2 : light.ew === 0 && light.ns === 2;
  }

  const groundMesh = ground.mesh(lambert);
  groundMesh.receiveShadow = true;
  const solidMesh = solid.mesh(lambert);
  solidMesh.castShadow = true;
  solidMesh.receiveShadow = true;
  const waterMesh = water.mesh(glassy);
  waterMesh.receiveShadow = true;
  scene.add(groundMesh, solidMesh, waterMesh);
  const lightsMesh = lights.mesh(lambert);
  lightsMesh.receiveShadow = true;
  const snowMesh = snowy.mesh(lambert);
  snowMesh.receiveShadow = true;
  const crownMeshes = {};
  for (const key in crowns) {
    crownMeshes[key] = crowns[key].mesh(lambert);
    crownMeshes[key].castShadow = crownMeshes[key].receiveShadow = true;
    scene.add(crownMeshes[key]);
  }
  scene.add(lightsMesh, snowMesh);

  // ---------------------------------------------------------------- 光
  const hemi = new THREE.HemisphereLight('#fffaf0', '#b9ad96', 1.9);
  scene.add(hemi);
  const sun = new THREE.DirectionalLight('#fff4e2', 2.1);
  const reach = Math.max(W, D) / 2 + 8;
  sun.position.set(-reach * 0.55, reach * 1.3, -reach * 0.35);
  sun.castShadow = true;
  sun.shadow.mapSize.set(touchy.matches ? 1024 : 2048, touchy.matches ? 1024 : 2048);
  Object.assign(sun.shadow.camera, { left: -reach, right: reach, top: reach, bottom: -reach, near: 1, far: reach * 4 });
  sun.shadow.bias = -0.0006;
  sun.shadow.normalBias = 0.03;
  scene.add(sun, sun.target);

  // ---------------------------------------------------------------- 云：几片方块云从西往东慢慢飘，地上有影子
  // 一片云：底下一块扁的，上面错落着几团鼓起来的，一层比一层小，像素风的那种「棉花糖」
  const cloudMat = new THREE.MeshLambertMaterial({ vertexColors: true, transparent: true, opacity: 0.78, depthWrite: false });
  const clouds = [];
  const cloudCount = Math.max(3, Math.round(W / 12));
  for (let i = 0; i < cloudCount; i++) {
    const b = blocks();
    const long = 2.6 + random() * 2.2, wide = 1.3 + random() * 0.6;
    const at = function (x0, y0, z0, x1, y1, z1) { b.add(x0 + W / 2, y0, z0 + D / 2, x1 + W / 2, y1, z1 + D / 2, P.cloud, 0.02); };
    at(0, 0, -wide / 2, long, 0.34, wide / 2);
    const bumps = 2 + Math.floor(random() * 2);
    for (let k = 0; k < bumps; k++) {
      const w = long * (0.32 + random() * 0.18), x = (long - w) * (k + 0.5) / bumps + (random() - 0.5) * 0.3;
      const h = 0.34 + 0.32 + random() * 0.3;
      at(x, 0.34, -wide * 0.38, x + w, h, wide * 0.38);
      at(x + w * 0.2, h, -wide * 0.24, x + w * 0.8, h + 0.24, wide * 0.24);
    }
    const mesh = b.mesh(cloudMat);
    mesh.castShadow = true;
    mesh.renderOrder = 1;                  // 比牌子先画：云飘过去也挡不住名字
    mesh.position.set(-W / 2 + random() * W, 15 + random() * 2.5, -D / 2 + random() * D * 0.7);
    scene.add(mesh);
    clouds.push({ mesh: mesh, speed: 0.22 + random() * 0.25, length: long });
  }
  function moveClouds(dt) {
    if (still.matches) return;
    for (const c of clouds) {
      c.mesh.position.x += c.speed * dt;
      if (c.mesh.position.x > W / 2 + 8) {
        c.mesh.position.x = -W / 2 - 8 - c.length;
        c.mesh.position.z = -D / 2 + random() * D;
      }
    }
  }

  // ---------------------------------------------------------------- 背景：白天、黄昏、夜晚……（welcome.SCENES）
  // 跟着欢迎条上的 data-kind（自动的按时间和季节挑，右上角能自己挑；换了 mascots.js 在欢迎条上发 scene:change）。
  // 每种：天光（半球光的天、地、强度），太阳 / 月亮（颜色、强度、从哪边照），东西整体压暗多少（下雨湿漉漉的），
  // 云的颜色和浓淡，灯亮不亮（多亮），天上掉什么，树冠用哪一套，小家伙戴什么，地上有没有雪。天空的颜色、星星月亮、
  // 彩虹在 style.css（按 .city 的 data-kind）
  const BASE = { sky: '#fffaf0', earth: '#b9ad96', hemi: 1.9, sun: '#fff4e2', power: 2.1, from: [-0.55, 1.3, -0.35],
                 tint: 1, cloud: '#ffffff', fog: 0.78, lamps: 0, fall: null, wear: null, crowns: 'day', snow: false };
  const SCENES = {
    day: {},
    dusk: { sky: '#ffd8b5', earth: '#9b8170', hemi: 1.35, sun: '#ffb27c', power: 1.9, from: [-1.25, 0.5, 0.2],
            cloud: '#ffd4c2', lamps: 0.35 },
    night: { sky: '#5f6f9e', earth: '#2b2d3b', hemi: 0.8, sun: '#a8bbff', power: 0.5, from: [0.6, 1.2, -0.5],
             cloud: '#5b6680', fog: 0.5, lamps: 0.6 },
    fireflies: { sky: '#5f7a9a', earth: '#2b3330', hemi: 0.8, sun: '#b4c8ff', power: 0.5, from: [0.6, 1.2, -0.5],
                 cloud: '#5b6680', fog: 0.5, lamps: 0.6, fall: 'fireflies' },
    rain: { sky: '#dfe4ea', earth: '#8d8e8a', hemi: 1.5, sun: '#e6ebf1', power: 0.75, tint: 0.9, cloud: '#9aa2ac',
            fog: 0.92, fall: 'rain', wear: 'umbrella' },
    rainbow: { sun: '#fff8ec', power: 2, from: [0.55, 1.3, -0.4] },
    snow: { sky: '#f3f6fb', earth: '#c6c9cf', hemi: 2, sun: '#f1f5ff', power: 1.6, cloud: '#eef1f5', fall: 'snow',
            wear: 'scarf', snow: true },
    sakura: { cloud: '#fff3f6', fall: 'petals', wear: 'petal', crowns: 'sakura' },
    autumn: { sky: '#fff2df', earth: '#b8a083', sun: '#ffe1b5', from: [-0.75, 1.05, -0.2], cloud: '#fff3e2',
              fall: 'leaves', wear: 'leaf', crowns: 'autumn' },
  };
  let sky = Object.assign({ kind: 'day' }, BASE);
  // 夜里亮着的窗户、灯泡：换一套暖黄的颜色（每扇深一点浅一点），不受光照
  const glowMat = new THREE.MeshBasicMaterial({ vertexColors: true });
  const glowColors = lightsMesh.geometry.attributes.color;
  const dayGlow = glowColors.array.slice(), nightGlow = new Float32Array(dayGlow.length);
  for (let v = 0; v < nightGlow.length / 3; v += 20) {        // 一个盒子五个面、二十个点
    color.set(P.glow[Math.floor(luck() * P.glow.length)]);
    for (let k = v; k < v + 20; k++) nightGlow.set([color.r, color.g, color.b], k * 3);
  }
  // 一团软软的光（萤火虫、雪花、路灯照在地上那一圈都用它）
  const softDot = (function () {
    const sheet = document.createElement('canvas');
    sheet.width = sheet.height = 32;
    const pen = sheet.getContext('2d'), fade = pen.createRadialGradient(16, 16, 0, 16, 16, 16);
    fade.addColorStop(0, 'rgba(255, 255, 255, 1)');
    fade.addColorStop(0.45, 'rgba(255, 255, 255, 0.8)');
    fade.addColorStop(1, 'rgba(255, 255, 255, 0)');
    pen.fillStyle = fade;
    pen.fillRect(0, 0, 32, 32);
    const texture = new THREE.CanvasTexture(sheet);
    texture.colorSpace = THREE.SRGBColorSpace;
    return texture;
  })();
  // 一道雨：斜斜的一笔，上面淡下面浓（WebGL 的线只有一个像素宽，看不清，用贴图画）
  const streak = (function () {
    const sheet = document.createElement('canvas');
    sheet.width = sheet.height = 32;
    const pen = sheet.getContext('2d'), fade = pen.createLinearGradient(0, 3, 0, 29);
    fade.addColorStop(0, 'rgba(255, 255, 255, 0)');
    fade.addColorStop(1, 'rgba(255, 255, 255, 1)');
    pen.strokeStyle = fade;
    pen.lineWidth = 2.2;
    pen.lineCap = 'round';
    pen.beginPath();
    pen.moveTo(19, 3);
    pen.lineTo(13, 29);
    pen.stroke();
    const texture = new THREE.CanvasTexture(sheet);
    texture.colorSpace = THREE.SRGBColorSpace;
    return texture;
  })();
  const lamps = data.props.filter(function (p) { return p.k === 'lamp'; });
  const lampPools = new THREE.InstancedMesh(new THREE.PlaneGeometry(2.6, 2.6).rotateX(-Math.PI / 2),
    new THREE.MeshBasicMaterial({ map: softDot, color: '#ffc46b', transparent: true, opacity: 0.6,
                                  blending: THREE.AdditiveBlending, depthWrite: false }), Math.max(1, lamps.length));
  lamps.forEach(function (p, i) {
    lampPools.setMatrixAt(i, new THREE.Matrix4().makeTranslation(X(p.x), WALK_TOP + 0.015, Z(p.z)));
  });
  lampPools.count = lamps.length;
  lampPools.visible = false;
  scene.add(lampPools);

  // 天上掉下来的：雨、雪、花瓣、落叶，夏夜里低低飘着的萤火虫。撒在整座城上空那一块（比城大一圈，从地面到云那么高），
  // 掉到地上从顶上再来；萤火虫绕着自己那一块慢慢飘、一闪一闪。手机上撒一半；减少动态效果时不撒
  const FALLS = {
    rain: { count: 900, top: 16, speed: [11, 15], colors: ['#6f86a1', '#8398b0'], size: 18, map: 'streak' },
    snow: { count: 650, top: 16, speed: [0.7, 1.3], sway: 0.45, colors: ['#ffffff'], size: 3.4, soft: true },
    petals: { count: 180, top: 11, speed: [0.5, 0.9], sway: 0.9, colors: P.sakura, size: 4 },
    leaves: { count: 150, top: 11, speed: [0.7, 1.2], sway: 0.8, colors: P.autumn, size: 4 },
    fireflies: { count: 80, top: 2.2, colors: ['#eaff8c', '#d8ff6e'], size: 7, soft: true, glow: true },
  };
  let weather = null;
  function setWeather(kind) {
    if (weather) {
      scene.remove(weather.object);
      weather.object.geometry.dispose();
      weather.object.material.dispose();
      weather = null;
    }
    const f = FALLS[kind];
    if (!f || still.matches) return;
    const n = Math.round(f.count * (touchy.matches ? 0.5 : 1));
    const pos = new Float32Array(n * 3), col = new Float32Array(n * 3), bits = [];
    for (let i = 0; i < n; i++) {
      color.set(f.colors[i % f.colors.length]);
      bits.push({ x: (Math.random() - 0.5) * (W + 8), y: Math.random() * f.top, z: (Math.random() - 0.5) * (D + 8),
                  v: f.speed ? f.speed[0] + Math.random() * (f.speed[1] - f.speed[0]) : 0, phase: Math.random() * 20,
                  r: color.r, g: color.g, b: color.b });
      col.set([color.r, color.g, color.b], i * 3);
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    geometry.setAttribute('color', new THREE.BufferAttribute(col, 3));
    const object = new THREE.Points(geometry, new THREE.PointsMaterial({
      size: f.size, sizeAttenuation: false, vertexColors: true, map: f.map === 'streak' ? streak : f.soft ? softDot : null,
      transparent: true, opacity: f.glow ? 1 : f.map === 'streak' ? 0.75 : 0.95, depthWrite: false,
      blending: f.glow ? THREE.AdditiveBlending : THREE.NormalBlending }));
    object.frustumCulled = false;
    scene.add(object);
    weather = { object: object, f: f, bits: bits, pos: pos, col: col, time: 0 };
    stepWeather(0);
  }
  function stepWeather(dt) {
    if (!weather) return;
    const f = weather.f, pos = weather.pos, col = weather.col, t = (weather.time += dt);
    for (let i = 0; i < weather.bits.length; i++) {
      const b = weather.bits[i];
      if (f.glow) {
        b.x += Math.cos(t * 0.6 + b.phase) * 0.35 * dt;
        b.z += Math.sin(t * 0.5 + b.phase * 1.3) * 0.35 * dt;
        b.y = 0.45 + (f.top - 0.45) * (0.5 + 0.5 * Math.sin(t * 0.4 + b.phase));
        const k = Math.pow(Math.max(0, Math.sin(t * 1.7 + b.phase * 2)), 3);
        col[i * 3] = b.r * k; col[i * 3 + 1] = b.g * k; col[i * 3 + 2] = b.b * k;
      } else {
        b.y -= b.v * dt;
        if (f.sway) {
          b.x += Math.sin(t * 1.1 + b.phase) * f.sway * dt;
          b.z += Math.cos(t * 0.9 + b.phase) * f.sway * 0.6 * dt;
        }
        if (b.y < 0) {
          b.y += f.top;
          b.x = (Math.random() - 0.5) * (W + 8);
          b.z = (Math.random() - 0.5) * (D + 8);
        }
      }
      pos[i * 3] = b.x;
      pos[i * 3 + 1] = b.y;
      pos[i * 3 + 2] = b.z;
    }
    weather.object.geometry.attributes.position.needsUpdate = true;
    if (f.glow) weather.object.geometry.attributes.color.needsUpdate = true;
  }
  const WEARS = ['umbrella', 'scarf', 'petal', 'leaf'];
  function setScene(kind) {
    const s = Object.assign({ kind: SCENES[kind] ? kind : 'day' }, BASE, SCENES[kind] || {});
    sky = s;
    hemi.color.set(s.sky);
    hemi.groundColor.set(s.earth);
    hemi.intensity = s.hemi;
    sun.color.set(s.sun);
    sun.intensity = s.power;
    sun.position.set(reach * s.from[0], reach * s.from[1], reach * s.from[2]);
    lambert.color.setScalar(s.tint);
    crewMat.emissive.set(s.lamps > 0.5 ? '#6b3a26' : s.lamps > 0 ? '#3a2014' : '#000000');
    cloudMat.color.set(s.cloud);
    cloudMat.opacity = focus ? 0.22 : s.fog;
    glowColors.array.set(s.lamps ? nightGlow : dayGlow);
    glowColors.needsUpdate = true;
    lightsMesh.material = s.lamps ? glowMat : lambert;
    lampPools.visible = s.lamps > 0;
    lampPools.material.opacity = s.lamps;
    snowMesh.visible = s.snow;
    for (const key in crownMeshes) crownMeshes[key].visible = key === s.crowns;
    for (const m of crew) for (const wear of WEARS) m.parts[wear].visible = s.wear === wear;
    host.dataset.kind = s.kind;
    setWeather(s.fall);
    requestRender();
  }

  // ---------------------------------------------------------------- 小家伙找路
  // 一张格子地图（一格 CELL 那么大）：只有人行道、斑马线、桥上的人行道、门口和广场的砖地走得通，别的（马路、草地、
  // 房子、河）都走不过去。每一趟用 A* 找一条，再拉直（两点之间直着走得通就直着走），拐角削圆一点
  const CELL = 0.2;
  const cols = Math.ceil(W / CELL), rows = Math.ceil(D / CELL);
  const walkable = new Uint8Array(cols * rows);
  const floorY = new Float32Array(cols * rows);
  function mark(x0, z0, x1, z1, y) {
    const a = Math.max(0, Math.floor(x0 / CELL)), b = Math.min(cols - 1, Math.ceil(x1 / CELL) - 1);
    const c = Math.max(0, Math.floor(z0 / CELL)), d = Math.min(rows - 1, Math.ceil(z1 / CELL) - 1);
    for (let j = c; j <= d; j++) {
      for (let i = a; i <= b; i++) { walkable[j * cols + i] = 1; floorY[j * cols + i] = y; }
    }
  }
  for (const r of of('walk')) mark(r.x, r.z, r.x + r.w, r.z + r.d, WALK_TOP);
  for (const r of crossings) mark(r.x, r.z, r.x + r.w, r.z + r.d, 0.015);
  for (const b of bridges) {
    mark(b.x, b.z + 0.1, b.x + b.w, b.z + 0.6, WALK_TOP);
    mark(b.x, b.z + b.d - 0.6, b.x + b.w, b.z + b.d - 0.1, WALK_TOP);
  }
  for (const r of of('plaza')) mark(r.x, r.z, r.x + r.w, r.z + r.d, PAVE_TOP);
  const cellOf = function (x, z) {
    return Math.max(0, Math.min(rows - 1, Math.floor(z / CELL))) * cols + Math.max(0, Math.min(cols - 1, Math.floor(x / CELL)));
  };
  for (const p of data.props) if (p.k === 'lamp' || p.k === 'signal') walkable[cellOf(p.x, p.z)] = 0;   // 杆子：绕着走
  // 斑马线上的格子：属于哪个路口（-1 不是斑马线）、往哪个方向过（1 横过南北向的马路，2 横过东西向的）
  const crossAt = new Int16Array(cols * rows).fill(-1), crossDir = new Uint8Array(cols * rows);
  for (const r of crossings) {
    const a = Math.max(0, Math.floor(r.x / CELL)), b = Math.min(cols - 1, Math.ceil((r.x + r.w) / CELL) - 1);
    const c = Math.max(0, Math.floor(r.z / CELL)), d = Math.min(rows - 1, Math.ceil((r.z + r.d) / CELL) - 1);
    for (let j = c; j <= d; j++) {
      for (let i = a; i <= b; i++) { crossAt[j * cols + i] = r.at || 0; crossDir[j * cols + i] = r.dir === 'x' ? 1 : 2; }
    }
  }
  function redAhead(m, time) {
    // 前面一小步是斑马线、自己还站在人行道上、这个方向还没放行：等着（已经走到斑马线上的走完）
    const ahead = along(m, Math.min(m.total, m.s + 0.3)), here = along(m, m.s);
    const next = cellOf(ahead[0], ahead[1]);
    if (crossAt[next] < 0 || crossAt[cellOf(here[0], here[1])] >= 0) return false;
    return !mayCross(crossAt[next], crossDir[next], time);
  }
  function nearestOpen(cell) {
    if (walkable[cell]) return cell;
    const ci = cell % cols, cj = Math.floor(cell / cols);
    for (let r = 1; r < 12; r++) {
      for (let j = cj - r; j <= cj + r; j++) {
        for (let i = ci - r; i <= ci + r; i++) {
          if (i < 0 || j < 0 || i >= cols || j >= rows) continue;
          if (walkable[j * cols + i]) return j * cols + i;
        }
      }
    }
    return cell;
  }
  function clear(ax, az, bx, bz) {
    // 两点之间直着走得通吗：每半格看一眼
    const n = Math.ceil(Math.hypot(bx - ax, bz - az) / (CELL / 2));
    for (let k = 1; k < n; k++) if (!walkable[cellOf(ax + (bx - ax) * k / n, az + (bz - az) * k / n)]) return false;
    return true;
  }
  function astar(from, to) {
    // 八个方向，斜着走不许擦着走不过去的格子的角
    const start = nearestOpen(cellOf(from[0], from[1])), goal = nearestOpen(cellOf(to[0], to[1]));
    const gi = goal % cols, gj = Math.floor(goal / cols);
    const cost = new Float32Array(cols * rows).fill(Infinity);
    const came = new Int32Array(cols * rows).fill(-1);
    const heap = [[0, start, 0]];         // [估计的总长, 格子, 走到这儿的长]
    cost[start] = 0;
    const push = function (item) {
      heap.push(item);
      let i = heap.length - 1;
      while (i > 0) {
        const up = (i - 1) >> 1;
        if (heap[up][0] <= heap[i][0]) break;
        [heap[up], heap[i]] = [heap[i], heap[up]];
        i = up;
      }
    };
    const pop = function () {
      const top = heap[0], last = heap.pop();
      if (heap.length) {
        heap[0] = last;
        let i = 0;
        for (;;) {
          const l = i * 2 + 1, r = l + 1;
          let m = i;
          if (l < heap.length && heap[l][0] < heap[m][0]) m = l;
          if (r < heap.length && heap[r][0] < heap[m][0]) m = r;
          if (m === i) break;
          [heap[m], heap[i]] = [heap[i], heap[m]];
          i = m;
        }
      }
      return top;
    };
    let found = false, budget = cols * rows;
    while (heap.length && budget > 0) {
      const top = pop(), cell = top[1];
      if (top[2] > cost[cell]) continue;   // 早先排进来的、后来找到了更近的走法：跳过，不算数
      budget--;
      if (cell === goal) { found = true; break; }
      const ci = cell % cols, cj = Math.floor(cell / cols);
      for (let dj = -1; dj <= 1; dj++) {
        for (let di = -1; di <= 1; di++) {
          if (!di && !dj) continue;
          const i = ci + di, j = cj + dj;
          if (i < 0 || j < 0 || i >= cols || j >= rows) continue;
          const next = j * cols + i;
          if (!walkable[next] || (di && dj && (!walkable[cj * cols + i] || !walkable[j * cols + ci]))) continue;
          const g = cost[cell] + (di && dj ? Math.SQRT2 : 1);
          if (g >= cost[next]) continue;
          cost[next] = g;
          came[next] = cell;
          const ddx = Math.abs(i - gi), ddz = Math.abs(j - gj);
          push([g + Math.max(ddx, ddz) + (Math.SQRT2 - 1) * Math.min(ddx, ddz), next, g]);
        }
      }
    }
    if (!found) return [from, to];          // 找不到（地图断开了）：直着走过去
    const cells = [];
    for (let c = goal; c !== -1; c = came[c]) cells.push(c);
    cells.reverse();
    const points = [from];
    for (let k = 1; k + 1 < cells.length; k++) {
      points.push([(cells[k] % cols + 0.5) * CELL, (Math.floor(cells[k] / cols) + 0.5) * CELL]);
    }
    points.push(to);
    return points;
  }
  function straighten(points) {
    // 拉直：从当前点往后找最远的、直着走得通的那个点
    const out = [points[0]];
    let i = 0;
    while (i < points.length - 1) {
      let j = points.length - 1;
      while (j > i + 1 && !clear(points[i][0], points[i][1], points[j][0], points[j][1])) j--;
      out.push(points[j]);
      i = j;
    }
    // 拐角削圆一点：拐点换成它前后各一小段（不超过 0.3 格）处的两个点；削了会走出人行道、斑马线的就不削，
    // 路一直跟着马路走
    if (out.length < 3) return out;
    const round = [out[0]];
    for (let k = 1; k + 1 < out.length; k++) {
      const a = out[k - 1], b = out[k], c = out[k + 1];
      const ab = Math.hypot(a[0] - b[0], a[1] - b[1]) || 1, cb = Math.hypot(c[0] - b[0], c[1] - b[1]) || 1;
      const ka = Math.min(0.3, ab / 2) / ab, kc = Math.min(0.3, cb / 2) / cb;
      const p = [b[0] + (a[0] - b[0]) * ka, b[1] + (a[1] - b[1]) * ka];
      const q = [b[0] + (c[0] - b[0]) * kc, b[1] + (c[1] - b[1]) * kc];
      if (clear(p[0], p[1], q[0], q[1])) round.push(p, q);
      else round.push(b);
    }
    round.push(out[out.length - 1]);
    return round;
  }
  function plan(from, to) {
    // 每一趟不太一样：一半的时候先绕到两头之间（框大一圈）随便哪一条斑马线再过去，走的街就不一样
    if (crossings.length && random() < 0.5) {
      const x0 = Math.min(from[0], to[0]) - 6, x1 = Math.max(from[0], to[0]) + 6;
      const z0 = Math.min(from[1], to[1]) - 6, z1 = Math.max(from[1], to[1]) + 6;
      const near = crossings.filter(function (c) {
        const cx = c.x + c.w / 2, cz = c.z + c.d / 2;
        return cx > x0 && cx < x1 && cz > z0 && cz < z1;
      });
      if (near.length) {
        const c = near[Math.floor(random() * near.length)];
        const via = [c.x + c.w / 2, c.z + c.d / 2];
        return straighten(astar(from, via).concat(astar(via, to).slice(1)));
      }
    }
    return straighten(astar(from, to));
  }

  // ---------------------------------------------------------------- 点公司楼：名下每个仓库走到这儿的路
  // 一路小方块（屋顶那个颜色）铺在人行道、斑马线、桥上，走最近的那条；有小家伙在搬的那几条，方块一点点往公司楼那头挪，
  // 没在搬的颜色浅一点、不动
  const ROUTE_GAP = 0.6;
  const dotGeo = new THREE.BoxGeometry(0.3, 0.04, 0.3);
  const dot = new THREE.Object3D();
  const routeCache = new Map();
  let trail = null;
  function routeOf(h, office) {
    if (!routeCache.has(h.id)) {
      const out = OUT[h.face] || [0, 1];
      const path = straighten(astar([h.door[0] + out[0] * 0.2, h.door[1] + out[1] * 0.2],
                                    [office.door[0], office.door[1] + 0.5]));
      const lengths = [0];
      for (let i = 1; i < path.length; i++) {
        lengths.push(lengths[i - 1] + Math.hypot(path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1]));
      }
      routeCache.set(h.id, { path: path, lengths: lengths, total: lengths[lengths.length - 1], busy: h.workers > 0 });
    }
    return routeCache.get(h.id);
  }
  function showRoutes(office) {
    // 铺好路，回镜头要框住的那些点（城里的坐标 [x, y, z]）；名下没有仓库回 null
    hideRoutes();
    const mine = data.houses.filter(function (h) { return h.office === office.id; });
    if (!mine.length) return null;
    const routes = mine.map(function (h) { return routeOf(h, office); });
    let count = 0;
    for (const r of routes) count += Math.floor(r.total / ROUTE_GAP) + 1;
    const mesh = new THREE.InstancedMesh(dotGeo, new THREE.MeshBasicMaterial({ color: '#ffffff' }), count);
    trail = { mesh: mesh, routes: routes, shift: 0, busy: new THREE.Color(office.roof),
              idle: new THREE.Color(office.roof).lerp(new THREE.Color('#ffffff'), 0.5) };
    layRoutes(0);
    scene.add(mesh);
    const points = [[office.x, 0, office.z], [office.x + office.w, office.h + 3, office.z + office.d]];   // 楼顶连牌子
    for (const h of mine) points.push([h.x, 0, h.z], [h.x + h.w, h.size[2] + 0.8, h.z + h.d]);
    for (const r of routes) for (const p of r.path) points.push([p[0], 0, p[1]]);
    return points;
  }
  function layRoutes(dt) {
    if (!trail) return;
    if (!still.matches) trail.shift = (trail.shift + dt * 1.1) % ROUTE_GAP;
    let n = 0;
    for (const r of trail.routes) {
      for (let s = r.busy ? trail.shift : 0; s <= r.total; s += ROUTE_GAP) {
        const p = along(r, s);
        dot.position.set(X(p[0]), floorY[nearestOpen(cellOf(p[0], p[1]))] + 0.03, Z(p[1]));
        dot.rotation.y = Math.atan2(p[2], p[3]);
        dot.updateMatrix();
        trail.mesh.setMatrixAt(n, dot.matrix);
        trail.mesh.setColorAt(n, r.busy ? trail.busy : trail.idle);
        n++;
      }
    }
    trail.mesh.count = n;
    trail.mesh.instanceMatrix.needsUpdate = true;
    if (trail.mesh.instanceColor) trail.mesh.instanceColor.needsUpdate = true;
  }
  function hideRoutes() {
    if (!trail) return;
    scene.remove(trail.mesh);
    trail.mesh.material.dispose();
    trail.mesh.dispose();
    trail = null;
  }

  // ---------------------------------------------------------------- 小家伙（和侧边栏那只一样的 10 × 8 格子，挤出 4 格厚）
  const V = 0.12;         // 一格多大（比真的比例大一点，远远看也认得出来）
  function sprite3d(parts) {
    const b = blocks();
    for (const q of parts) b.add(q[0] * V + W / 2, q[1] * V, q[2] * V + D / 2, q[3] * V + W / 2, q[4] * V, q[5] * V + D / 2, q[6], 0);
    return b.mesh(lambert).geometry;
  }
  // 和页面上一样横着走：脸（+z）朝着镜头，往 +x 走；眼睛看着要去的那边（格子的第 3、8 列），往回走整个翻过来。
  // x 是左右（-5 ~ 5），y 往上（0 ~ 8），z 是前后（-2 ~ 2）
  const bodyGeo = sprite3d([
    [-4, 2, -2, 4, 8, 2, P.mascot],
    [-2, 6, 2, -1, 7, 2.3, P.eye], [3, 6, 2, 4, 7, 2.3, P.eye],
  ]);
  const armsDownGeo = sprite3d([[-5, 4, -0.5, -4, 6, 0.5, P.mascot], [4, 4, -0.5, 5, 6, 0.5, P.mascot]]);
  const armsUpGeo = sprite3d([[-5, 6, -0.5, -4, 9, 0.5, P.mascot], [4, 6, -0.5, 5, 9, 0.5, P.mascot]]);
  // 四条腿（格子的第 1、3、6、8 列）：两对轮流抬起一格，和页面上走路的那两帧一样
  const legsAGeo = sprite3d([[-4, 0, -1, -3, 2, 1, P.mascot], [1, 0, -1, 2, 2, 1, P.mascot],
                              [-2, 1, -1, -1, 2, 1, P.mascot], [3, 1, -1, 4, 2, 1, P.mascot]]);
  const legsBGeo = sprite3d([[-4, 1, -1, -3, 2, 1, P.mascot], [1, 1, -1, 2, 2, 1, P.mascot],
                              [-2, 0, -1, -1, 2, 1, P.mascot], [3, 0, -1, 4, 2, 1, P.mascot]]);
  const cargoGeo = sprite3d([[-2.6, 9, -2.2, 2.6, 13.4, 2.2, P.box], [-2.7, 10.9, -2.3, 2.7, 11.5, 2.3, P.tape]]);
  // 跟着背景戴的：下雨撑伞（杆子从身子旁边伸上去，伞面在箱子上头，三种颜色轮着），下雪围巾，樱花、秋天头上一片
  const umbrellaGeos = P.umbrella.map(function (hex) {
    return sprite3d([[5.2, 4, -0.3, 5.8, 15, 0.3, P.post], [-6, 14.6, -4, 6, 15.4, 4, hex], [-4, 15.4, -2.6, 4, 16, 2.6, hex],
                     [-1.2, 16, -0.6, 1.2, 16.5, 0.6, hex]]);
  });
  const scarfGeo = sprite3d([[-4.2, 4.2, -2.2, 4.2, 5.2, 2.2, P.scarf], [1.6, 2.4, 2.2, 2.8, 4.2, 2.5, P.scarf]]);
  const petalGeo = sprite3d([[0.6, 8, -0.7, 2.2, 8.5, 0.7, P.sakura[0]]]);
  const leafGeo = sprite3d([[0.6, 8, -0.7, 2.4, 8.5, 0.7, P.autumn[0]]]);

  // 仓库门口、公司楼门口外面一点：小家伙从这儿出来、到这儿进去
  const OUT = { e: [1, 0], w: [-1, 0], s: [0, 1], n: [0, -1] };
  // 小家伙自己一份材质：黄昏、夜里自己微微发亮一点（月光太暗，不然只剩一团黑影）
  const crewMat = new THREE.MeshLambertMaterial({ vertexColors: true });
  const officeById = new Map(data.offices.map(function (o) { return [o.id, o]; }));
  const crew = [], crewMeshes = [];
  for (const h of data.houses) {
    const office = h.workers && h.office ? officeById.get(h.office) : null;
    if (!office) continue;
    const out = OUT[h.face] || [0, 1];
    const home = [h.door[0] + out[0] * 0.2, h.door[1] + out[1] * 0.2];
    const workers = touchy.matches ? 1 : h.workers;      // 手机上一个仓库一只就够了
    for (let n = 0; n < workers; n++) {
      const group = new THREE.Group();
      const parts = {
        body: new THREE.Mesh(bodyGeo, crewMat), armsDown: new THREE.Mesh(armsDownGeo, crewMat),
        armsUp: new THREE.Mesh(armsUpGeo, crewMat), legsA: new THREE.Mesh(legsAGeo, crewMat),
        legsB: new THREE.Mesh(legsBGeo, crewMat), cargo: new THREE.Mesh(cargoGeo, crewMat),
        umbrella: new THREE.Mesh(umbrellaGeos[crew.length % umbrellaGeos.length], crewMat),
        scarf: new THREE.Mesh(scarfGeo, crewMat), petal: new THREE.Mesh(petalGeo, crewMat), leaf: new THREE.Mesh(leafGeo, crewMat),
      };
      const m = { group: group, parts: parts, home: home, office: office, house: h, speed: SPEED * (h.speed || 1),
                  stepRate: 6 + random() * 2, phase: random() * 10, state: 'inside', wait: 0, carrying: true,
                  path: null, lengths: null, total: 0, s: 0, pauseAt: -1, pauseLeft: 0, look: 1, trips: 0 };
      for (const key in parts) {
        parts[key].castShadow = true;
        parts[key].userData.crew = m;
        group.add(parts[key]);
        crewMeshes.push(parts[key]);
      }
      parts.umbrella.visible = parts.scarf.visible = parts.petal.visible = parts.leaf.visible = false;
      group.visible = false;
      scene.add(group);
      // 一开始错开：有的在路上走到一半，有的还在仓库里
      if (n % 2 === 0) {
        walk(m, true);
        m.s = random() * m.total;
      } else {
        m.wait = random() * 3;
      }
      crew.push(m);
    }
  }
  function walk(m, carrying) {
    // 出发：去的时候从仓库门口到公司楼门口（门口左右错开一点，几只不挤在一个点上），回来反过来
    const door = [m.office.door[0] + (random() - 0.5) * 1.2, m.office.door[1] + 0.5];
    const path = carrying ? plan(m.home, door) : plan(door, m.home);
    const lengths = [0];
    for (let i = 1; i < path.length; i++) {
      lengths.push(lengths[i - 1] + Math.hypot(path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1]));
    }
    Object.assign(m, { state: 'walking', carrying: carrying, path: path, lengths: lengths,
                       total: lengths[lengths.length - 1], s: 0, pace: m.speed * (0.85 + random() * 0.3),
                       pauseAt: -1, pauseLeft: 0 });
    // 三成的时候走到半路停下来张望一会儿
    if (random() < 0.3) m.pauseAt = m.total * (0.3 + random() * 0.4);
  }
  function along(m, s) {
    // 路上 s 格的地方、朝哪个方向
    const r = m.path, L = m.lengths;
    let i = 1;
    while (i < L.length - 1 && L[i] < s) i++;
    const span = L[i] - L[i - 1] || 1;
    const t = Math.max(0, Math.min(1, (s - L[i - 1]) / span));
    const ax = r[i - 1][0], az = r[i - 1][1], bx = r[i][0], bz = r[i][1];
    return [ax + (bx - ax) * t, az + (bz - az) * t, bx - ax, bz - az];
  }

  let lastTime = null;
  function moveCrew(time) {
    const dt = lastTime === null ? 0 : Math.min(0.1, Math.max(0, time - lastTime));
    lastTime = time;
    moveClouds(dt);
    stepWeather(dt);
    layRoutes(dt);
    paintSignals(time);
    for (const m of crew) {
      const parts = m.parts;
      if (still.matches) {
        // 不动：举着箱子站在仓库门口
        if (!m.path) walk(m, true);
        place(m, along(m, 0), true, 1);
        continue;
      }
      if (m.state === 'inside') {
        // 在仓库里装箱子、在公司楼里卸货：等一会儿再出来
        m.group.visible = false;
        m.wait -= dt;
        if (m.wait <= 0) walk(m, m.carrying);
        continue;
      }
      m.waiting = m.pauseLeft <= 0 && redAhead(m, time);
      if (m.waiting) {
        // 路口红灯：站在马路牙子上等，左看看、右看看
        m.waitFor = (m.waitFor || 0) + dt;
        m.look = Math.floor(m.waitFor / 0.6) % 2 ? -1 : 1;
      } else if (m.pauseLeft > 0) {
        // 停下来张望：左看看、右看看
        m.pauseLeft -= dt;
        if (Math.floor(m.pauseLeft / 0.45) % 2 !== Math.floor((m.pauseLeft + dt) / 0.45) % 2) m.look = -m.look;
      } else {
        m.waitFor = 0;
        m.s += m.pace * dt;
        if (m.pauseAt >= 0 && m.s >= m.pauseAt) {
          m.pauseLeft = 0.9 + random() * 1.1;
          m.pauseAt = -1;
          m.look = 1;
        }
      }
      if (m.s >= m.total) {
        // 到了：钻进门里，一会儿空着手（或者又装了一箱）出来
        m.state = 'inside';
        m.wait = m.carrying ? 0.9 + random() * 0.6 : 0.8 + random() * 1.4;
        if (m.carrying) m.trips += 1;
        m.carrying = !m.carrying;
        m.group.visible = false;
        continue;
      }
      // 进门出门那一下缩一缩
      const edge = Math.min(m.s, m.total - m.s);
      const p = along(m, m.s);
      if (m.pauseLeft > 0 || m.waiting) { p[2] *= m.look; p[3] *= m.look; }
      place(m, p, m.carrying, Math.min(1, 0.45 + edge / 0.5));
      const step = m.pauseLeft > 0 || m.waiting ? 0 : Math.floor(time * m.stepRate + m.phase) % 2;
      parts.legsA.visible = step === 0;
      parts.legsB.visible = step === 1;
      m.group.position.y += step ? 0.025 : 0;
    }
  }
  function place(m, p, carrying, grow) {
    const parts = m.parts;
    const len = Math.hypot(p[2], p[3]) || 1;
    const dx = p[2] / len, dz = p[3] / len;
    m.group.position.set(X(p[0]), floorY[cellOf(p[0], p[1])] || WALK_TOP, Z(p[1]));
    // 脸朝着镜头那一边（走的方向转 90°，挑朝镜头的那个），往哪边走就看哪边、身子翻过去
    let fx = dz, fz = -dx;
    if (fx * Math.sin(view.yaw) + fz * Math.cos(view.yaw) < 0) { fx = -fx; fz = -fz; }
    m.group.rotation.y = Math.atan2(fx, fz);
    const ahead = dx * fz - dz * fx > 0 ? 1 : -1;         // 走的方向在本地 +x 那边是 1
    m.group.scale.set(grow * ahead, grow, grow);
    m.group.visible = true;
    parts.cargo.visible = carrying;
    parts.armsUp.visible = carrying;
    parts.armsDown.visible = !carrying;
    if (still.matches) { parts.legsA.visible = true; parts.legsB.visible = false; }
  }
  function doing(m) {
    // 这只小家伙在干什么（提示、卡片上写的）
    if (m.state === 'inside') return m.carrying ? '在仓库里装箱子' : '在' + m.office.name + '的楼里卸货';
    if (m.waiting) return '在路口等红灯';
    if (m.pauseLeft > 0) return '停下来张望一下';
    return m.carrying ? '举着箱子去' + m.office.name : '空着手回仓库';
  }

  // ---------------------------------------------------------------- 镜头
  const camera = new THREE.OrthographicCamera(-10, 10, 10, -10, 0.1, 1000);
  const renderer = new THREE.WebGLRenderer({ canvas: canvas, context: context, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.shadowMap.enabled = !touchy.matches;          // 手机上不画影子
  renderer.shadowMap.type = THREE.PCFShadowMap;
  renderer.setClearColor(0x000000, 0);
  // 镜头对着地面上的一点（view.x、view.z，three.js 的坐标），绕着它转；zoom 1 是整个城刚好框在画布里，
  // 这时对着的是 home（框的正中，城往右让开左上角的欢迎语）。滚轮朝着鼠标那一点放大；缩回 1 倍的路上慢慢回到 home
  const view = { yaw: YAW, zoom: 1, x: 0, z: 0, tilt: ELEVATION };
  let home = { x: 0, z: 0 };
  const MAX_ZOOM = 4, MIN_ZOOM = 0.7;      // 按钮能缩到 0.7（转过去以后看全）；滚轮缩到 1 就不缩了，接着滚页面

  function aim() {
    const r = 200;
    camera.position.set(view.x + Math.sin(view.yaw) * Math.cos(view.tilt) * r, Math.sin(view.tilt) * r,
                        view.z + Math.cos(view.yaw) * Math.cos(view.tilt) * r);
    camera.up.set(0, 1, 0);
    camera.lookAt(view.x, 0, view.z);
    camera.zoom = view.zoom;
    camera.updateProjectionMatrix();
    sizeBoards();
  }
  function fit() {
    const width = canvas.clientWidth || 1, height = canvas.clientHeight || 1;
    renderer.setSize(width, height, false);
    // 默认角度下城的外轮廓（底座的角、楼顶连牌子）投到屏幕上，框住它们，再按画布的宽高比放宽窄的那一边
    const right = new THREE.Vector3(Math.cos(YAW), 0, -Math.sin(YAW));
    const up = new THREE.Vector3(-Math.sin(ELEVATION) * Math.sin(YAW), Math.cos(ELEVATION),
                                 -Math.sin(ELEVATION) * Math.cos(YAW));
    let u0 = Infinity, u1 = -Infinity, v0 = Infinity, v1 = -Infinity;
    const p = new THREE.Vector3();
    for (const q of hull) {
      p.set(X(q[0]), q[1], Z(q[2]));
      const u = p.dot(right), v = p.dot(up);
      u0 = Math.min(u0, u); u1 = Math.max(u1, u); v0 = Math.min(v0, v); v1 = Math.max(v1, v);
    }
    const pad = 0.5;
    u0 -= pad; u1 += pad; v0 -= pad; v1 += pad;
    const aspect = width / height;
    let w = u1 - u0, h = v1 - v0;
    if (w / h < aspect) {
      // 左右有空：多出来的放一些在左边，城往右挪，让开左上角的欢迎语
      const extra = h * aspect - w;
      const who = welcome.querySelector('.welcome-who');
      const covered = who ? Math.min(0.45, (who.offsetWidth + 14) / width) * h * aspect : 0;
      const left = Math.min(extra, Math.max(extra / 2, covered * 0.6));
      u0 -= left; u1 += extra - left; w = h * aspect;
    } else {
      // 上下有空（窄屏）：多出来的放一些在上面，城往下挪，让开顶上的欢迎语
      const extra = w / aspect - h;
      const who = welcome.querySelector('.welcome-who');
      const covered = who ? Math.min(0.6, (who.offsetTop + who.offsetHeight + 8) / height) * w / aspect : 0;
      const top = Math.min(extra, Math.max(extra / 2, covered));
      v1 += top; v0 -= extra - top; h = w / aspect;
    }
    // 框的正中对着地面上的哪一点：地面上的 (x, 0, z) 投到屏幕上是 u = x cos θ − z sin θ、v = −sin φ (x sin θ + z cos θ)
    const cu = (u0 + u1) / 2, k = -(v0 + v1) / 2 / Math.sin(ELEVATION);
    home = { x: cu * Math.cos(YAW) + k * Math.sin(YAW), z: -cu * Math.sin(YAW) + k * Math.cos(YAW) };
    Object.assign(camera, { left: -w / 2, right: w / 2, top: h / 2, bottom: -h / 2 });
    if (view.zoom === 1 && !focus) { view.x = home.x; view.z = home.z; }
    aim();
  }
  // 鼠标下面是地面上的哪一点（滚轮朝着它放大）
  function groundAt(x, y) {
    const rect = canvas.getBoundingClientRect();
    pointer.set((x - rect.left) / rect.width * 2 - 1, -((y - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    const ray = raycaster.ray;
    if (Math.abs(ray.direction.y) < 1e-6) return null;
    const t = -ray.origin.y / ray.direction.y;
    return t > 0 ? ray.origin.clone().addScaledVector(ray.direction, t) : null;
  }
  // 放大到 next 倍：at 是放大时停在屏幕上原处不动的那一点；缩小时镜头按比例往 home 收，缩回 1 倍正好回到 home。
  // 没变（到头了）回 false
  function zoomTo(next, at, floor) {
    next = Math.max(floor, Math.min(MAX_ZOOM, next));
    const before = view.zoom;
    if (Math.abs(next - before) < 1e-4) return false;
    tween = null;
    if (next < before) {
      const keep = before > 1 ? Math.max(0, Math.min(1, (next - 1) / (before - 1))) : 0;
      view.x = home.x + (view.x - home.x) * keep;
      view.z = home.z + (view.z - home.z) * keep;
    } else if (at) {
      const pull = 1 - before / next;
      view.x += (at.x - view.x) * pull;
      view.z += (at.z - view.z) * pull;
    }
    view.x = Math.max(-W / 2, Math.min(W / 2, view.x));
    view.z = Math.max(-D / 2, Math.min(D / 2, view.z));
    view.zoom = next;
    aim();
    requestRender();
    return true;
  }
  // 镜头慢慢飞到 to（yaw、zoom、x、z）：特写、飞回去都走这个；减少动态效果时直接到
  let tween = null;
  function fly(to, ms) {
    const from = { yaw: view.yaw, zoom: view.zoom, x: view.x, z: view.z, tilt: view.tilt };
    const target = Object.assign({}, from, to);
    // 转的时候走近的那一边（差不超过半圈）
    while (target.yaw - from.yaw > Math.PI) target.yaw -= Math.PI * 2;
    while (target.yaw - from.yaw < -Math.PI) target.yaw += Math.PI * 2;
    if (still.matches) {
      Object.assign(view, target);
      aim();
      requestRender();
      return;
    }
    tween = { from: from, to: target, start: performance.now(), ms: ms || 900 };
    requestRender();
  }
  function stepTween(now) {
    if (!tween) return;
    const t = Math.min(1, (now - tween.start) / tween.ms);
    const e = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
    const a = tween.from, b = tween.to;
    view.yaw = a.yaw + (b.yaw - a.yaw) * e;
    view.zoom = a.zoom * Math.pow(b.zoom / a.zoom, e);
    view.x = a.x + (b.x - a.x) * e;
    view.z = a.z + (b.z - a.z) * e;
    view.tilt = a.tilt + (b.tilt - a.tilt) * e;
    aim();
    if (t >= 1) tween = null;
  }
  // 从 yaw、tilt 这个角度框住这些点（城里的坐标 [x, y, z]）：对着地面上哪一点、放大几倍。卡片挡着的那一块
  // （电脑上是右边，手机上是底下）让开，框在空着的那一块正中
  function frame(points, yaw, tilt) {
    const right = new THREE.Vector3(Math.cos(yaw), 0, -Math.sin(yaw));
    const up = new THREE.Vector3(-Math.sin(tilt) * Math.sin(yaw), Math.cos(tilt), -Math.sin(tilt) * Math.cos(yaw));
    let u0 = Infinity, u1 = -Infinity, v0 = Infinity, v1 = -Infinity;
    const p = new THREE.Vector3();
    for (const q of points) {
      p.set(X(q[0]), q[1], Z(q[2]));
      const u = p.dot(right), v = p.dot(up);
      u0 = Math.min(u0, u); u1 = Math.max(u1, u); v0 = Math.min(v0, v); v1 = Math.max(v1, v);
    }
    const width = canvas.clientWidth || 1, height = canvas.clientHeight || 1;
    let side = 0, below = 0;
    if (!card.hidden) {
      // 卡片盖在城的右边（电脑）、底下，就让开它；手机上卡片接在城下面，不用让
      const c = canvas.getBoundingClientRect(), k = card.getBoundingClientRect();
      if (k.top < c.bottom && k.left > c.left + c.width * 0.4) side = c.right - k.left + 14;
      else if (k.top < c.bottom && k.bottom > c.top) below = c.bottom - k.top + 10;
    }
    const fullW = camera.right - camera.left, fullH = camera.top - camera.bottom;     // 放大 1 倍时看得见多大
    const pad = 2;
    const zoom = Math.max(MIN_ZOOM, 0.92 * Math.min(FOCUS_ZOOM, fullW * (width - side) / width / (u1 - u0 + pad * 2),
                                                    fullH * (height - below) / height / (v1 - v0 + pad * 2)));
    const cu = (u0 + u1) / 2 + side / 2 / width * fullW / zoom;
    const cv = (v0 + v1) / 2 - below / 2 / height * fullH / zoom;
    const k = -cv / Math.sin(tilt);
    return { x: cu * Math.cos(yaw) + k * Math.sin(yaw), z: -cu * Math.sin(yaw) + k * Math.cos(yaw), zoom: zoom };
  }

  // ---------------------------------------------------------------- 画
  let running = false, dirty = true, last = 0, seen = true;
  const began = performance.now();
  function requestRender() { dirty = true; wake(); }
  function wake() {
    if (running || !seen || document.hidden) return;
    running = true;
    requestAnimationFrame(tick);
  }
  function tick(now) {
    running = false;
    if (!seen || document.hidden) return;
    const moving = (crew.length || clouds.length) && !still.matches;
    // 小家伙在走：一秒画 30 下就够了；拖着转、镜头在飞的时候每帧都画
    if (dirty || dragging || tween || (moving && now - last >= 32)) {
      last = now;
      stepTween(now);
      moveCrew((now - began) / 1000);
      follow();
      talkFollow();
      renderer.render(scene, camera);
      veilFollow();
      dirty = false;
    }
    if (moving || dragging || dirty || tween || bubble) wake();
  }

  // ---------------------------------------------------------------- 悬浮
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let hovered = null, dragging = false, dragFrom = null, moved = false;
  let tip = null;
  function tipBox() {
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'chart-tip city-tip';
      tip.hidden = true;
      document.body.append(tip);
    }
    return tip;
  }
  function tipOf(item) {
    // 小家伙的提示是现编的：哪个仓库的、在干什么
    if (item.kind !== 'crew') return item.tip;
    const m = item.crew;
    return { head: m.house.card.title + ' 的小家伙', total: doing(m), rows: [['送到', m.office.name]],
             foot: '点一下镜头跟着它' };
  }
  function showTip(item, x, y) {
    const data = tipOf(item);
    const box = tipBox();
    box.replaceChildren();
    const head = document.createElement('div');
    head.className = 'tip-head';
    head.textContent = data.head;
    box.append(head);
    if (data.total) {
      const total = document.createElement('div');
      total.className = 'tip-total';
      total.textContent = data.total;
      box.append(total);
    }
    if (data.rows && data.rows.length) {
      const table = document.createElement('table');
      table.className = 'tip-table';
      for (const row of data.rows) {
        const tr = table.insertRow();
        tr.insertCell().textContent = row[0];
        const value = tr.insertCell();
        value.className = 'tip-num';
        value.textContent = row[1];
      }
      box.append(table);
    }
    if (data.foot) {
      const foot = document.createElement('div');
      foot.className = 'tip-foot';
      foot.textContent = data.foot;
      box.append(foot);
    }
    box.hidden = false;
    let left = x + 16, top = y - box.offsetHeight - 14;
    if (left + box.offsetWidth > window.innerWidth - 8) left = x - box.offsetWidth - 16;
    if (top < 8) top = y + 18;
    box.style.left = Math.max(8, left) + 'px';
    box.style.top = top + 'px';
  }
  function hideTip() { if (tip) tip.hidden = true; }
  function itemAt(x, y) {
    // 先看是不是点着小家伙（它们会动，用它们自己的方块），再看房子、小公园那些看不见的盒子
    const rect = canvas.getBoundingClientRect();
    pointer.set((x - rect.left) / rect.width * 2 - 1, -((y - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    const mascot = raycaster.intersectObjects(crewMeshes.filter(function (mesh) { return mesh.visible && mesh.parent.visible; }), false)[0];
    if (mascot) {
      const m = mascot.object.userData.crew;
      return m.item || (m.item = { kind: 'crew', crew: m, sign: null, url: m.house.url });
    }
    const hit = raycaster.intersectObjects(picks, false)[0];
    return hit ? hit.object.userData : null;
  }
  function hover(item, x, y) {
    if (item !== hovered) {
      hovered = item;
      sizeBoards();                       // 放上去的那块牌子大一点
      canvas.style.cursor = item ? 'pointer' : 'grab';
      requestRender();
    }
    if (item) showTip(item, x, y); else hideTip();
  }

  // ---------------------------------------------------------------- 点开：镜头飞过去看特写，右边一张卡片
  const veil = host.querySelector('[data-city-veil]');
  const card = host.querySelector('[data-city-card]');
  let focus = null, before = null;
  const FACING = { e: Math.PI / 2, w: -Math.PI / 2, s: 0, n: Math.PI };
  function open(item) {
    hideTip();
    if (!focus) before = { yaw: view.yaw, zoom: view.zoom, x: view.x, z: view.z, tilt: view.tilt };
    focus = { item: item };
    fill(item);
    card.hidden = false;                  // 先把卡片放上去：镜头要让开它
    welcome.classList.add('has-city-card');
    // 公司楼：拉远一点，框住它和名下所有的仓库，地上铺出每个仓库走到这儿的路；别的客户的牌子淡下去
    hideRoutes();
    const points = item.kind === 'office' ? showRoutes(officeById.get(item.id)) : null;
    if (points) {
      const to = frame(points, view.yaw, FOCUS_TILT);
      fly({ zoom: to.zoom, x: to.x, z: to.z, tilt: FOCUS_TILT }, 900);
    } else if (item.kind === 'crew') {
      const p = item.crew.group.position;
      fly({ zoom: Math.max(view.zoom, FOCUS_ZOOM + 0.4), x: p.x, z: p.z, tilt: FOCUS_TILT }, 800);
    } else {
      // 对着门那一面、偏一点斜着看；高的楼放大少一点，整栋装得下
      const at = item.at;
      const turn = item.face && item.face in FACING ? FACING[item.face] + 0.5 : view.yaw;
      const zoom = item.kind === 'office' ? Math.max(1.6, FOCUS_ZOOM - item.height * 0.12) : FOCUS_ZOOM;
      fly({ yaw: turn, zoom: zoom, x: X(at[0]), z: Z(at[2]), tilt: item.kind === 'office' ? ELEVATION + 0.1 : FOCUS_TILT }, 900);
    }
    veil.hidden = !!points;               // 路铺满城，不盖那层白
    for (const s of boards) s.material.opacity = points && s.userData.owner !== item.id ? 0.35 : 1;
    host.classList.add('is-focused');
    cloudMat.opacity = 0.22;              // 特写时云淡一点，不挡着看
    sizeBoards();
    requestRender();
  }
  function unfocus() {
    // 收起特写：卡片、那层白、路线拿掉，牌子都亮回来，云恢复这个背景的浓淡
    focus = null;
    card.hidden = true;
    welcome.classList.remove('has-city-card');
    veil.hidden = true;
    hideRoutes();
    for (const s of boards) s.material.opacity = 1;
    host.classList.remove('is-focused');
    cloudMat.opacity = sky.fog;
  }
  function close() {
    if (!focus) return;
    unfocus();
    if (before) fly(before, 800);
    before = null;
    sizeBoards();
    requestRender();
  }
  function follow() {
    // 跟着小家伙：镜头慢慢追上它（它进了门就停在门口等它出来）；卡片上的「在干什么」跟着变
    if (!focus || focus.item.kind !== 'crew') return;
    const m = focus.item.crew;
    if (m.group.visible && !tween) {
      view.x += (m.group.position.x - view.x) * 0.12;
      view.z += (m.group.position.z - view.z) * 0.12;
      aim();
    }
    const now = card.querySelector('[data-doing]');
    if (now && now.textContent !== doing(m)) now.textContent = doing(m);
  }
  function veilFollow() {
    // 别的地方淡下去：一层半透明的白，特写的那一栋（那一只）那儿挖一个洞
    if (!focus) return;
    const item = focus.item;
    const p = item.kind === 'crew' ? item.crew.group.position.clone().setY(item.crew.group.position.y + 0.6)
      : new THREE.Vector3(X(item.at[0]), item.at[1], Z(item.at[2]));
    p.project(camera);
    veil.style.setProperty('--vx', ((p.x + 1) / 2 * canvas.clientWidth).toFixed(0) + 'px');
    veil.style.setProperty('--vy', ((1 - p.y) / 2 * canvas.clientHeight).toFixed(0) + 'px');
    veil.style.setProperty('--vr', (item.kind === 'office' ? 150 : item.kind === 'crew' ? 70 : 110) + 'px');
  }
  function fill(item) {
    // 卡片：标题、副标题、大字（余额 / 累计消费），使用率那一条，几行数，近 14 天调用的小柱子，按钮
    const info = item.kind === 'crew' ? item.crew.house.card : item.card;
    card.replaceChildren();
    const shut = document.createElement('button');
    shut.type = 'button';
    shut.className = 'city-card-close';
    shut.setAttribute('aria-label', '关掉，飞回去');
    shut.textContent = '✕';
    shut.addEventListener('click', close);
    const title = document.createElement('p');
    title.className = 'city-card-title';
    title.textContent = item.kind === 'crew' ? info.title + ' 的小家伙' : info.title;
    const sub = document.createElement('p');
    sub.className = 'city-card-sub';
    sub.textContent = item.kind === 'crew' ? '送到 ' + item.crew.office.name : info.sub;
    card.append(shut, title, sub);
    if (item.kind === 'crew') {
      const now = document.createElement('p');
      now.className = 'city-card-big';
      now.dataset.doing = '';
      now.textContent = doing(item.crew);
      const trips = document.createElement('p');
      trips.className = 'city-card-note';
      trips.textContent = item.crew.trips ? '这次打开以来送了 ' + item.crew.trips + ' 箱' : '刚上班';
      card.append(now, trips);
    } else {
      const big = document.createElement('p');
      big.className = 'city-card-big';
      big.textContent = info.big;
      if (info.big_label) {
        const small = document.createElement('small');
        small.textContent = ' ' + info.big_label;
        big.append(small);
      }
      card.append(big);
    }
    if (info.pct !== null && info.pct !== undefined && item.kind !== 'crew') {
      const bar = document.createElement('div');
      bar.className = 'meter city-card-meter tone-' + info.tone;
      const fillBar = document.createElement('i');
      fillBar.style.width = Math.max(2, Math.min(100, info.pct)) + '%';
      bar.append(fillBar);
      card.append(bar);
    }
    const facts = document.createElement('dl');
    facts.className = 'city-card-facts';
    for (const row of info.facts || []) {
      const dt = document.createElement('dt');
      dt.textContent = row[0];
      const dd = document.createElement('dd');
      dd.textContent = row[1];
      facts.append(dt, dd);
    }
    card.append(facts);
    if (info.note && item.kind !== 'crew') {
      const note = document.createElement('p');
      note.className = 'city-card-note';
      note.textContent = info.note;
      card.append(note);
    }
    if (info.calls14 && info.calls14.length && item.kind !== 'crew') {
      const most = Math.max.apply(null, info.calls14.concat([1]));
      const spark = document.createElement('div');
      spark.className = 'city-card-spark';
      spark.setAttribute('aria-label', '近 14 天每天的调用次数');
      for (const value of info.calls14) {
        const bit = document.createElement('i');
        bit.style.height = Math.max(4, value / most * 100) + '%';
        bit.title = value.toLocaleString('en-US') + ' 次';
        spark.append(bit);
      }
      const label = document.createElement('p');
      label.className = 'city-card-note';
      label.textContent = '近 14 天调用，最后一根是今天';
      card.append(spark, label);
    }
    const go = document.createElement('a');
    go.className = 'btn btn-sm city-card-go';
    go.href = info.url;
    go.textContent = info.link + ' →';
    card.append(go);
  }
  document.addEventListener('keydown', function (event) { if (event.key === 'Escape' && focus) close(); });

  // ---------------------------------------------------------------- 拖、点、滚
  // 左键拖着绕城转，右键拖着平移：城跟着鼠标走（鼠标下面那块地一直在鼠标下面）；不拖就是点
  canvas.addEventListener('pointerdown', function (event) {
    if (event.button !== 0 && event.button !== 2) return;
    tween = null;
    dragFrom = { x: event.clientX, y: event.clientY, yaw: view.yaw, vx: view.x, vz: view.z,
                 pan: event.button === 2 };
    moved = false;
    if (event.pointerType !== 'touch') {
      try { canvas.setPointerCapture(event.pointerId); } catch (error) { /* 指针已经没了：不拖也照样能点 */ }
    }
  });
  canvas.addEventListener('pointermove', function (event) {
    if (dragFrom && event.pointerType !== 'touch') {
      const dx = event.clientX - dragFrom.x;
      if (!moved && Math.abs(dx) + Math.abs(event.clientY - dragFrom.y) > 4) {
        moved = true;
        dragging = true;
        hover(null);
        canvas.style.cursor = 'grabbing';
      }
      if (moved && dragFrom.pan) {
        pan(dx, event.clientY - dragFrom.y);
        return;
      }
      if (moved) {
        view.yaw = dragFrom.yaw - dx * 0.005;
        aim();
        requestRender();
        return;
      }
    }
    if (event.pointerType === 'touch') return;
    hover(itemAt(event.clientX, event.clientY), event.clientX, event.clientY);
  });
  canvas.addEventListener('pointerup', function (event) {
    const wasDrag = moved;
    dragFrom = null;
    moved = false;
    if (dragging) { dragging = false; canvas.style.cursor = hovered ? 'pointer' : 'grab'; }
    if (wasDrag || event.button !== 0) return;
    const item = itemAt(event.clientX, event.clientY);
    if (item) open(item);
    else close();                         // 点空白处：飞回去
  });
  canvas.addEventListener('pointercancel', function () { dragFrom = null; moved = false; dragging = false; });
  canvas.addEventListener('contextmenu', function (event) { event.preventDefault(); });    // 右键是平移，不弹菜单
  function pan(dx, dy) {
    // 屏幕上挪 1 个像素 = 地面上挪多少：往右是镜头的右边，往下是离镜头远的那个方向（再除一下俯角的 sin）
    const per = (camera.right - camera.left) / view.zoom / (canvas.clientWidth || 1);
    const s = Math.sin(view.yaw), c = Math.cos(view.yaw), deep = per / Math.sin(view.tilt);
    view.x = Math.max(-W / 2, Math.min(W / 2, dragFrom.vx - c * dx * per - s * dy * deep));
    view.z = Math.max(-D / 2, Math.min(D / 2, dragFrom.vz + s * dx * per - c * dy * deep));
    aim();
    requestRender();
  }
  canvas.addEventListener('pointerleave', function () { if (!dragFrom) hover(null); });
  window.addEventListener('scroll', function () { if (hovered) hover(null); }, { passive: true });

  // 滚轮：朝着鼠标那一点放大、缩小。缩回 1 倍（整个城）以后再往下滚就是滚页面，不会卡在城里
  canvas.addEventListener('wheel', function (event) {
    const factor = Math.exp(-event.deltaY * (event.deltaMode === 1 ? 0.05 : 0.0018));
    if (!zoomTo(view.zoom * factor, groundAt(event.clientX, event.clientY), Math.min(1, view.zoom))) return;
    event.preventDefault();
    hover(null);
  }, { passive: false });

  for (const button of host.querySelectorAll('[data-city-zoom]')) {
    button.addEventListener('click', function () {
      const how = button.dataset.cityZoom;
      if (how === 'in') zoomTo(view.zoom * 1.3, null, MIN_ZOOM);
      else if (how === 'out') zoomTo(view.zoom / 1.3, null, MIN_ZOOM);
      else {
        if (focus) {
          unfocus();
          before = null;
          sizeBoards();
        }
        fly({ zoom: 1, yaw: YAW, x: home.x, z: home.z, tilt: ELEVATION }, 700);
      }
    });
  }

  // ---------------------------------------------------------------- 小家伙说话
  const talkSource = document.getElementById('mascot-talk');
  const talk = talkSource ? JSON.parse(talkSource.textContent) : null;
  let bubble = null, talker = null, talkUntil = 0, recent = [];
  function line() {
    const moods = Object.keys(talk.moods || {});
    const pools = [talk.shared || [], talk.context || []];
    if (moods.length) pools.push(talk.moods[moods[Math.floor(Math.random() * moods.length)]]);
    const weatherLines = (talk.scenes || {})[sky.kind];
    if (weatherLines && weatherLines.length) pools.push(weatherLines, weatherLines);     // 跟着背景说的多说一点
    pools.push(['这一箱是 token。', '搬完这箱就去喝水。', '老板说今天要搬完。', '箱子比我还大。',
                '我在送货，别挡道。', '这家调用好多！', '走人行道，安全第一。']);
    for (let tries = 0; tries < 8; tries++) {
      const pool = pools[Math.floor(Math.random() * pools.length)];
      const text = pool[Math.floor(Math.random() * pool.length)];
      if (text && recent.indexOf(text) < 0) {
        recent.push(text);
        if (recent.length > 10) recent.shift();
        return text;
      }
    }
    return '嗨！';
  }
  function talkFollow() {
    if (!bubble) return;
    if (performance.now() > talkUntil || !talker || !talker.group.visible) {
      bubble.remove();
      bubble = null;
      talker = null;
      return;
    }
    const head = new THREE.Vector3();
    talker.group.getWorldPosition(head);
    head.y += 1.45;
    head.project(camera);
    bubble.style.left = ((head.x + 1) / 2 * canvas.clientWidth) + 'px';
    bubble.style.top = ((1 - head.y) / 2 * canvas.clientHeight) + 'px';
  }
  function chat() {
    const busy = document.querySelector('.pet-bubble:not([hidden])');
    const walking = crew.filter(function (m) { return m.group.visible; });
    if (talk && !bubble && !busy && !document.hidden && seen && walking.length) {
      talker = walking[Math.floor(Math.random() * walking.length)];
      bubble = document.createElement('span');
      bubble.className = 'mascot-bubble city-bubble';
      bubble.textContent = line();
      host.append(bubble);
      talkUntil = performance.now() + 3200;
      talkFollow();
      wake();
    }
    setTimeout(chat, 22000 + Math.random() * 20000);
  }
  if (!still.matches) setTimeout(chat, 6000 + Math.random() * 8000);

  // ---------------------------------------------------------------- 开始
  fit();
  const stage = welcome.querySelector('[data-mascot-stage]');
  setScene(stage ? stage.dataset.kind : 'day');
  if (stage) stage.addEventListener('scene:change', function (event) { setScene(event.detail); });
  new ResizeObserver(function () { fit(); requestRender(); }).observe(host);
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(function (entries) {
      seen = entries[entries.length - 1].isIntersecting;
      if (seen) wake();
    }).observe(host);
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { dirty = true; wake(); } });
  canvas.style.cursor = 'grab';
  host.classList.add('is-ready');
  requestRender();
}
