/* 几个页面共用的小交互：右上角弹窗（也给页面脚本一个 appToast）、可排序的表、
 * 账号选择器、时间范围下拉、折线 / 柱状 / 气泡图的悬浮和拖动。
 * 页面专属的脚本（概览的筛选排序、账号管理的筛选和弹窗）还是写在各自的模板里。
 * 所有从服务器拿到的名字（模型 ID、邮箱、标签）一律当不可信文本，用 textContent 插入。 */
(function () {
  'use strict';

  // ------------------------------------------------------------ 右上角弹窗
  // × 关掉；操作结果（data-auto）6 秒后自己走，鼠标停在上面时不走；
  // 「查看详情」展开 / 收起原始报错
  const AUTO_MS = 6000;

  function dismiss(toast) {
    if (toast.classList.contains('is-leaving')) return;
    toast.classList.add('is-leaving');
    const done = function () { toast.remove(); };
    toast.addEventListener('animationend', done, { once: true });
    setTimeout(done, 400);   // 系统关了动画时 animationend 不会来
  }

  function autoDismiss(toast) {
    let timer = setTimeout(function () { dismiss(toast); }, AUTO_MS);
    toast.addEventListener('mouseenter', function () { clearTimeout(timer); });
    toast.addEventListener('mouseleave', function () {
      timer = setTimeout(function () { dismiss(toast); }, AUTO_MS / 2);
    });
  }

  const stack = document.getElementById('toasts');
  if (stack) {
    stack.addEventListener('click', function (event) {
      const close = event.target.closest('.toast-x');
      if (close) { dismiss(close.closest('.toast')); return; }
      const more = event.target.closest('[data-toast-detail]');
      if (more) {
        const box = document.getElementById(more.getAttribute('aria-controls'));
        if (!box) return;
        box.hidden = !box.hidden;
        more.setAttribute('aria-expanded', String(!box.hidden));
        more.textContent = box.hidden ? '查看详情' : '收起详情';
      }
    });
    for (const toast of stack.querySelectorAll('.toast[data-auto]')) autoDismiss(toast);
  }

  // 页面脚本里弹一条（fetch 存完之后说结果）：照 shell.html 里的模板克隆。
  // 最新的放最上面；文字一律 textContent，不拼 HTML
  // 和服务端的 flash_result 一样三段：粗体一句话、后面灰字说是哪个账号、下面一行补充说明
  window.appToast = function (tone, title, text, sub) {
    const template = document.querySelector('template[data-toast-template="' + tone + '"]');
    if (!stack || !template) return;
    const toast = template.content.firstElementChild.cloneNode(true);
    toast.querySelector('.toast-title').textContent = title;
    if (sub) {
      const who = document.createElement('span');
      who.className = 'toast-sub';
      who.textContent = sub;
      toast.querySelector('.toast-head').append(who);
    }
    if (text) {
      const line = document.createElement('p');
      line.className = 'toast-text';
      line.textContent = text;
      toast.querySelector('.toast-body').append(line);
    }
    stack.prepend(toast);
    if (toast.hasAttribute('data-auto')) autoDismiss(toast);
  };

  // ------------------------------------------------------------ 「刷新数据」
  // 点了之后图标一直转（CSS 认 .is-busy），等新页面回来；点完又按后退（bfcache 原样端回来）就停。
  // 用 Ctrl / ⌘ 点是在新标签页打开，这一页不等
  document.addEventListener('click', function (event) {
    const link = event.target instanceof Element ? event.target.closest('[data-refresh]') : null;
    if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    link.classList.add('is-busy');
  });
  window.addEventListener('pageshow', function () {
    for (const link of document.querySelectorAll('[data-refresh].is-busy')) link.classList.remove('is-busy');
  });

  // ------------------------------------------------------------ 滚进屏幕才放进场动画
  // 带 data-reveal 的卡片（客户页）：里面的进场动画先停在第一帧（CSS 暂停），卡片滚进屏幕时
  // 加上 .in 再放；数字从 0 数上来也等到那时候。没有 IntersectionObserver 的浏览器直接放
  const revealWaits = new Map();   // 卡片 -> 滚进来时要做的事
  const revealIo = 'IntersectionObserver' in window ? new IntersectionObserver(function (entries) {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      entry.target.classList.add('in');
      revealIo.unobserve(entry.target);
      for (const run of revealWaits.get(entry.target) || []) run();
      revealWaits.delete(entry.target);
    }
  }, { threshold: 0.12 }) : null;
  for (const box of document.querySelectorAll('[data-reveal]')) {
    if (revealIo) revealIo.observe(box); else box.classList.add('in');
  }
  function whenRevealed(el, run) {
    const box = el.closest('[data-reveal]');
    if (!box || box.classList.contains('in') || !revealIo) { run(); return; }
    if (!revealWaits.has(box)) revealWaits.set(box, []);
    revealWaits.get(box).push(run);
  }

  // ------------------------------------------------------------ 数字从 0 数上来
  // 额度仪表中间的大数字和使用率，跟着弧一起动（同样 1.2 秒、同一条缓动）。只在系统没开
  // 「减弱动画」时做；最后一帧换回服务端写好的原文，格式一个字都不差
  if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    const ease = function (t) { return 1 - Math.pow(1 - t, 3); };
    for (const el of document.querySelectorAll('[data-countup]')) {
      const final = el.textContent.trim();
      const parts = final.match(/^([^\d]*)([\d,]+(?:\.(\d+))?)(.*)$/);
      if (!parts) continue;
      const target = Number(parts[2].replace(/,/g, ''));
      if (!isFinite(target) || target === 0) continue;
      const digits = parts[3] ? parts[3].length : 0;
      const format = function (value) {
        return parts[1] + value.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits }) + parts[4];
      };
      el.textContent = format(0);
      whenRevealed(el, function () {
        const begin = performance.now() + 100;   // 和 CSS 里的 .1s 延迟对齐
        const step = function (now) {
          const t = Math.max(0, Math.min(1, (now - begin) / 1200));
          if (t < 1) {
            el.textContent = format(target * ease(t));
            requestAnimationFrame(step);
          } else {
            el.textContent = final;
          }
        };
        requestAnimationFrame(step);
      });
    }
  }

  // ------------------------------------------------------------ 可排序的表
  // <table data-sortable>：表头 th.sortable 点一次升、再点降、第三次回到服务端给的顺序。
  // 显示的文字带千分位、「—」「未设置」之类，所以格子上的 data-sort 是排序用的规范值；
  // data-sort-type="number" 按数字比。空值永远沉底，不跟着升降翻转
  for (const table of document.querySelectorAll('table[data-sortable]')) {
    const body = table.tBodies[0];
    const heads = [...table.querySelectorAll('thead th.sortable')];
    const original = body ? [...body.rows] : [];
    if (original.length < 2) continue;
    const value = function (row, index) {
      const cell = row.cells[index];
      if (!cell) return '';
      const raw = cell.dataset.sort;
      return raw === undefined ? cell.textContent.trim() : raw;
    };
    const reorder = function (index, type, sign) {
      // 每次都从原始顺序重排：「降序」是原始顺序的真反转，同值行的相对次序也稳定
      const rows = [...original];
      rows.sort(function (a, b) {
        const x = value(a, index), y = value(b, index);
        if (!x && !y) return 0;
        if (!x) return 1;
        if (!y) return -1;
        if (type === 'number') return sign * (Number(x) - Number(y));
        return sign * x.localeCompare(y, 'zh-CN', { numeric: true });
      });
      body.append(...rows);
    };
    for (const head of heads) {
      const go = function () {
        const current = head.getAttribute('aria-sort');
        const next = current === 'none' ? 'ascending' : current === 'ascending' ? 'descending' : 'none';
        for (const other of heads) other.setAttribute('aria-sort', 'none');
        head.setAttribute('aria-sort', next);
        if (next === 'none') {
          body.append(...original);
          table.classList.remove('is-sorted');
          return;
        }
        table.classList.add('is-sorted');
        reorder(head.cellIndex, head.dataset.sortType, next === 'ascending' ? 1 : -1);
      };
      head.addEventListener('click', go);
      head.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); go(); }
      });
    }
  }

  // ------------------------------------------------------------ 账号选择器
  // 原生 <select> 一直在表单里，选中值也只认它；这里只是换一张脸：
  // 按钮显示头像 + ID + 邮箱，弹层里能搜。选了之后改 select 的值并触发 change，
  // 筛选表单「改动即提交」的那段脚本会接着提交。
  function initPicker(root) {
    const select = root.querySelector('select');
    const button = root.querySelector('.acct-picker-btn');
    const pop = root.querySelector('.acct-picker-pop');
    const search = root.querySelector('.acct-picker-search');
    const options = [...root.querySelectorAll('.acct-opt')];
    const groups = [...root.querySelectorAll('.acct-picker-group')];
    const empty = root.querySelector('.acct-picker-empty');
    if (!select || !button || !pop || !search) return;
    button.hidden = false;
    root.classList.add('is-ready');
    let active = -1;

    const shown = function () { return options.filter(function (o) { return !o.hidden; }); };

    function highlight(index) {
      const list = shown();
      for (const o of options) o.classList.remove('is-active');
      if (!list.length) { active = -1; search.removeAttribute('aria-activedescendant'); return; }
      active = (index + list.length) % list.length;
      const option = list[active];
      option.classList.add('is-active');
      search.setAttribute('aria-activedescendant', option.id);
      option.scrollIntoView({ block: 'nearest' });
    }

    function filter() {
      const query = search.value.trim().toLowerCase();
      for (const o of options) o.hidden = !!query && !o.dataset.search.toLowerCase().includes(query);
      for (const g of groups) {
        g.hidden = !options.some(function (o) { return o.dataset.group === g.dataset.group && !o.hidden; });
      }
      if (empty) empty.hidden = shown().length > 0;
    }

    function open() {
      pop.hidden = false;
      button.setAttribute('aria-expanded', 'true');
      search.value = '';
      filter();
      const current = shown().findIndex(function (o) { return o.getAttribute('aria-selected') === 'true'; });
      highlight(current < 0 ? 0 : current);
      search.focus();
    }

    function close(refocus) {
      pop.hidden = true;
      button.setAttribute('aria-expanded', 'false');
      if (refocus) button.focus();
    }

    function choose(option) {
      if (!option) return;
      close(true);
      if (select.value === option.dataset.value) return;
      // 账号页页头上的那个：直接跳到那个账号的同一个页签
      if (root.dataset.href) {
        window.location.href = root.dataset.href.replace('__ID__', encodeURIComponent(option.dataset.number));
        return;
      }
      select.value = option.dataset.value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
    }

    button.addEventListener('click', function () { if (pop.hidden) open(); else close(false); });
    search.addEventListener('input', function () { filter(); highlight(0); });
    search.addEventListener('keydown', function (event) {
      if (event.key === 'ArrowDown') { event.preventDefault(); highlight(active + 1); }
      else if (event.key === 'ArrowUp') { event.preventDefault(); highlight(active - 1); }
      else if (event.key === 'Enter') { event.preventDefault(); choose(shown()[active]); }
      else if (event.key === 'Escape') { event.preventDefault(); close(true); }
      else if (event.key === 'Tab') { close(false); }
    });
    for (const option of options) {
      option.addEventListener('click', function () { choose(option); });
      option.addEventListener('mousemove', function () {
        const index = shown().indexOf(option);
        if (index !== active) highlight(index);
      });
    }
    document.addEventListener('mousedown', function (event) {
      if (!pop.hidden && !root.contains(event.target)) close(false);
    });
  }
  for (const root of document.querySelectorAll('[data-picker]')) initPicker(root);

  // ------------------------------------------------------------ 时间范围下拉
  // 原生 <details> 自己会开合，这里只补「点外面 / 按 Esc 收起来」
  const ranges = [...document.querySelectorAll('[data-range-picker]')];
  if (ranges.length) {
    document.addEventListener('mousedown', function (event) {
      for (const box of ranges) if (box.open && !box.contains(event.target)) box.open = false;
    });
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      for (const box of ranges) {
        if (box.open) { box.open = false; box.querySelector('summary').focus(); }
      }
    });
  }

  // ------------------------------------------------------------ 图的悬浮提示
  // 一个提示框全页共用。值在前、名字在后，键用短线不用色块；值已经在服务端
  // 写成了要显示的样子（金额、次数），这里原样放。
  let tip = null;
  function tipBox() {
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'chart-tip';
      tip.hidden = true;
      document.body.append(tip);
    }
    return tip;
  }
  function hideTip() { if (tip) tip.hidden = true; }

  function fillTip(head, total, rows, foot) {
    const box = tipBox();
    box.replaceChildren();
    const top = document.createElement('div');
    top.className = 'tip-head';
    top.textContent = head;
    box.append(top);
    if (total) {
      const sum = document.createElement('div');
      sum.className = 'tip-total';
      sum.textContent = total;
      box.append(sum);
    }
    if (rows.length) {
      const table = document.createElement('table');
      table.className = 'tip-table';
      for (const row of rows) {
        const tr = table.insertRow();
        const name = tr.insertCell();
        const key = document.createElement('i');
        key.className = 'line-key';
        key.style.background = row.color;
        name.append(key, document.createTextNode(row.name));
        const value = tr.insertCell();
        value.className = 'tip-num';
        value.textContent = row.value;
      }
      box.append(table);
    }
    if (foot) {
      const note = document.createElement('div');
      note.className = 'tip-foot';
      note.textContent = foot;
      box.append(note);
    }
    box.hidden = false;
  }

  function placeTip(rect) {
    const box = tipBox();
    let left = rect.left + rect.width / 2 - box.offsetWidth / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - box.offsetWidth - 8));
    let top = rect.top - box.offsetHeight - 10;
    if (top < 8) top = rect.bottom + 10;
    box.style.left = left + 'px';
    box.style.top = top + 'px';
  }
  window.addEventListener('scroll', hideTip, { passive: true });

  // ---- 折线图：一条虚线准线跟着鼠标找最近的时间点，每条线上一个带白圈的点，
  //      提示里列出这一刻的全部序列（值在前、名字在后，大的在上）。
  //      也能 Tab 进图里用左右方向键逐点看。数据见 chart.render_lines 的 tooltip
  for (const holder of document.querySelectorAll('[data-line-src]')) {
    const source = document.getElementById(holder.dataset.lineSrc);
    const svg = holder.querySelector('.line-chart');
    if (!source || !svg) continue;
    const data = JSON.parse(source.textContent);
    const overlay = svg.querySelector('.chart-overlay');
    const cross = svg.querySelector('.chart-cross');
    const dots = [...svg.querySelectorAll('.chart-focus')];
    if (!overlay || !cross || !data.x || !data.x.length) continue;
    const count = data.x.length;
    const number = function (v) { return Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 }); };
    let current = -1;

    // 放大了（见下面的「沿横轴放大、拖动」）：时间点的位置跟着换算
    const zx = function (x) { return svg.zoomX ? svg.zoomX(x) : x; };
    const nearest = function (clientX) {
      const box = svg.getBoundingClientRect();
      const x = (clientX - box.left) * svg.viewBox.baseVal.width / (box.width || 1);
      let best = 0, gap = Infinity;
      data.x.forEach(function (value, i) {
        const d = Math.abs(zx(value) - x);
        if (d < gap) { gap = d; best = i; }
      });
      return best;
    };

    const show = function (index) {
      current = index;
      const x = zx(data.x[index]);
      cross.setAttribute('transform', 'translate(' + x + ',0)');
      cross.style.opacity = '1';
      const rows = data.series.map(function (s) { return { name: s.name, color: s.color, value: s.v[index], y: s.y[index] }; });
      rows.forEach(function (row, i) {
        const dot = dots[i];
        if (!dot) return;
        dot.setAttribute('cx', x);
        dot.setAttribute('cy', row.y);
        dot.setAttribute('fill', row.color);
        dot.style.opacity = row.value > 0 ? '1' : '0';
      });
      const live = rows.filter(function (r) { return r.value > 0; }).sort(function (a, b) { return b.value - a.value; });
      const total = rows.reduce(function (sum, r) { return sum + r.value; }, 0);
      fillTip(data.labels[index], '合计 ' + number(total) + ' ' + data.unit,
              live.map(function (r) { return { name: r.name, color: r.color, value: number(r.value) }; }),
              live.length ? '' : '这个时间点没有调用');
      // 提示挨着准线、和这一刻最高的那个点齐平，右边放不下就放左边
      const box = svg.getBoundingClientRect();
      const scale = box.width / svg.viewBox.baseVal.width;
      const tip = tipBox();
      const anchor = live.length ? Math.min.apply(null, live.map(function (r) { return r.y; })) : svg.viewBox.baseVal.height / 2;
      let left = box.left + x * scale + 16;
      if (left + tip.offsetWidth > window.innerWidth - 8) left = box.left + x * scale - tip.offsetWidth - 16;
      const top = Math.min(box.top + anchor * scale - tip.offsetHeight / 2, window.innerHeight - tip.offsetHeight - 8);
      tip.style.left = Math.max(8, left) + 'px';
      tip.style.top = Math.max(8, top) + 'px';
    };

    const hide = function () {
      hideTip();
      cross.style.opacity = '0';
      dots.forEach(function (d) { d.style.opacity = '0'; });
    };

    overlay.addEventListener('mousemove', function (e) { show(nearest(e.clientX)); });
    overlay.addEventListener('mouseleave', hide);
    overlay.addEventListener('focus', function () { show(current < 0 ? count - 1 : current); });
    overlay.addEventListener('blur', hide);
    overlay.addEventListener('keydown', function (e) {
      let index = current < 0 ? count - 1 : current;
      if (e.key === 'ArrowLeft') index -= 1;
      else if (e.key === 'ArrowRight') index += 1;
      else if (e.key === 'Home') index = 0;
      else if (e.key === 'End') index = count - 1;
      else if (e.key === 'Escape') { hide(); return; }
      else return;
      e.preventDefault();
      show(Math.max(0, Math.min(count - 1, index)));
    });
  }

  // ---- 堆叠柱状图：划到哪一格那一格亮、其余变淡；点图例只看那一项
  for (const holder of document.querySelectorAll('[data-tip-src]')) {
    const source = document.getElementById(holder.dataset.tipSrc);
    const svg = holder.querySelector('.bar-chart');
    if (!source || !svg) continue;
    const data = JSON.parse(source.textContent);
    const columns = [...svg.querySelectorAll('.bar-col')];
    let focus = null;   // 图例选中的那一项（序号），null = 全看

    const leave = function () {
      svg.classList.remove('is-hovering');
      for (const col of columns) col.classList.remove('is-hover');
      hideTip();
    };
    for (const hit of svg.querySelectorAll('.chart-hit')) {
      const show = function () {
        const index = Number(hit.dataset.idx);
        const item = data[index];
        if (!item) return;
        svg.classList.add('is-hovering');
        columns.forEach(function (col, i) { col.classList.toggle('is-hover', i === index); });
        const rows = focus === null ? item.rows : item.rows.filter(function (r) { return r.s === focus; });
        fillTip(item.label, '合计 ' + item.total, rows);
        placeTip((columns[index] || hit).getBoundingClientRect());
      };
      hit.addEventListener('mouseenter', show);
      hit.addEventListener('focus', show);
      hit.addEventListener('mouseleave', leave);
      hit.addEventListener('blur', leave);
    }

    const legend = document.querySelector('[data-legend-for="' + holder.id + '"]');
    if (!legend) continue;
    const toggles = [...legend.querySelectorAll('.legend-toggle')];
    legend.addEventListener('click', function (event) {
      const button = event.target.closest('.legend-toggle');
      if (!button) return;
      const index = Number(button.dataset.s);
      focus = focus === index ? null : index;
      for (const t of toggles) t.setAttribute('aria-pressed', String(Number(t.dataset.s) === focus));
      legend.classList.toggle('has-focus', focus !== null);
      if (focus === null) {
        delete svg.dataset.focus;
      } else {
        svg.dataset.focus = String(focus);
      }
      for (const seg of svg.querySelectorAll('[data-s]')) {
        seg.classList.toggle('is-focus', Number(seg.dataset.s) === focus);
      }
    });
  }

  // ------------------------------------------------------------ 图：沿横轴放大、拖动
  // 带时间轴的图（柱子、折线、面积、还能用几天）在 svg 上写了 data-zoom="左,右"（绘图区的横向范围，
  // viewBox 单位，见 chart._zoom_attrs）。放大只拉横轴：纵轴、字的大小都不变，柱子变宽、线拉长，
  // 横轴的字跟着挪，放得下就多显示几个。重新算的是每个图形的横坐标（不是整体缩放），所以线不会变粗、
  // 圆点不会变扁。怎么用：鼠标划到图上滚滚轮，往上放大、往下缩小（以鼠标为中心；缩到全貌还往下滚、
  // 放到最大还往上滚，就照常滚页面）；手机上双指捏合；放大以后按住拖、或者横着滑，左右看；双击回到全貌。
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const calm = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let zoomSeq = 0;

  function mapPath(d, map) {
    let isX = true;
    // chart.py 画的路径只有绝对坐标的 M / L / Q / C / Z，数字两两一组（横、纵）
    return d.replace(/[A-Za-z]|-?\d*\.?\d+(?:e[-+]?\d+)?/g, function (token) {
      if (/^[A-Za-z]$/.test(token)) { isX = true; return token; }
      const out = isX ? map(Number(token)).toFixed(2) : token;
      isX = !isX;
      return out;
    });
  }
  function mapPoints(points, map) {
    return points.replace(/(-?\d*\.?\d+),(-?\d*\.?\d+)/g, function (all, x, y) {
      return map(Number(x)).toFixed(2) + ',' + y;
    });
  }

  function zoomable(svg) {
    const edges = svg.dataset.zoom.split(',').map(Number);
    const x0 = edges[0], x1 = edges[1], span = x1 - x0;
    if (!(span > 0)) return;
    const count = Number(svg.dataset.zoomN) || 1;
    const band = Number(svg.dataset.zoomBand) || 0;
    const gap = Number(svg.dataset.zoomGap) || 0;
    const kMax = Math.max(2, Math.min(24, count / 4));
    const state = { k: 1, off: 0 };          // 放大倍数；往左拖过去多少（viewBox 单位，0 ~ span·(k−1)）
    const map = function (x) { return x0 + (x - x0) * state.k - state.off; };
    svg.zoomX = function (x) { return state.k === 1 ? x : map(x); };

    // 记下每个要挪的图形原来的横坐标。纵轴和横线不动；折线图的准线、焦点由悬浮脚本按 zoomX 放
    const grid = svg.querySelector('.chart-grid');
    const items = [];
    for (const el of svg.querySelectorAll('rect, path, polyline, polygon, circle, line, text')) {
      // 线尾的头像不挪：放大时整组藏起来（style.css 的 .is-zoomed）
      if ((grid && grid.contains(el)) || el.closest('defs, .chart-cursor, .chart-endavatars')) continue;
      const tag = el.tagName.toLowerCase();
      const item = { el: el, tag: tag };
      if (tag === 'rect') item.attrs = ['x', 'width'];
      else if (tag === 'path') item.attrs = ['d'];
      else if (tag === 'polyline' || tag === 'polygon') item.attrs = ['points'];
      else if (tag === 'circle') item.attrs = ['cx'];
      else if (tag === 'line') item.attrs = ['x1', 'x2'];
      else item.attrs = ['x', 'display'];
      item.orig = item.attrs.map(function (name) { return el.getAttribute(name); });
      item.axis = !!el.closest('.chart-xaxis');
      item.ends = !!el.closest('.chart-endlabels');
      item.index = el.dataset.i === undefined ? null : Number(el.dataset.i);
      items.push(item);
    }
    // 画图的那几层放大时裁到绘图区里（纵轴、横轴的字不裁）
    const layers = [...svg.children].filter(function (child) {
      return !child.matches('defs, .chart-grid, .chart-xaxis, title, desc');
    });
    const clipId = 'zoom-clip-' + (++zoomSeq);
    let defs = svg.querySelector('defs');
    if (!defs) { defs = document.createElementNS(SVG_NS, 'defs'); svg.prepend(defs); }
    const clip = document.createElementNS(SVG_NS, 'clipPath');
    clip.id = clipId;
    const clipRect = document.createElementNS(SVG_NS, 'rect');
    clipRect.setAttribute('x', x0 - 1);
    clipRect.setAttribute('y', 0);
    clipRect.setAttribute('width', span + 2);
    clipRect.setAttribute('height', svg.viewBox.baseVal.height);
    clip.append(clipRect);
    defs.append(clip);

    function draw() {
      const zoomed = state.k > 1.0001;
      const stride = band > 0 && gap > 0 ? Math.max(1, Math.ceil(gap / (band * state.k))) : 0;
      for (const it of items) {
        const el = it.el;
        if (!zoomed) {                       // 回到全貌：原样放回服务端画的
          it.attrs.forEach(function (name, i) {
            if (it.orig[i] === null) el.removeAttribute(name); else el.setAttribute(name, it.orig[i]);
          });
          if (it.tag === 'line') el.style.display = '';
          continue;
        }
        if (it.tag === 'rect') {
          el.setAttribute('x', map(Number(it.orig[0])).toFixed(2));
          el.setAttribute('width', (Number(it.orig[1]) * state.k).toFixed(2));
        } else if (it.tag === 'path') {
          el.setAttribute('d', mapPath(it.orig[0] || '', map));
        } else if (it.tag === 'polyline' || it.tag === 'polygon') {
          el.setAttribute('points', mapPoints(it.orig[0] || '', map));
        } else if (it.tag === 'circle') {
          el.setAttribute('cx', map(Number(it.orig[0])).toFixed(2));
        } else if (it.tag === 'line') {
          const a = map(Number(it.orig[0])), b = map(Number(it.orig[1]));
          el.setAttribute('x1', a.toFixed(2));
          el.setAttribute('x2', b.toFixed(2));
          if (it.axis) el.style.display = a < x0 - 0.5 || a > x1 + 0.5 ? 'none' : '';
        } else {                             // 字：挪位置；横轴的出了绘图区就藏，放得下就多显示几个
          const x = map(Number(it.orig[0]));
          el.setAttribute('x', x.toFixed(2));
          let show = it.orig[1] !== 'none';
          if (it.index !== null && stride) show = it.index % stride === 0;
          if (it.ends) show = false;           // 线尾直标的是全貌里的末值，放大了就不标
          else if (it.axis) show = show && x >= x0 - 0.5 && x <= x1 + 0.5;
          if (show) el.removeAttribute('display'); else el.setAttribute('display', 'none');
        }
      }
      for (const layer of layers) {
        if (zoomed) layer.setAttribute('clip-path', 'url(#' + clipId + ')'); else layer.removeAttribute('clip-path');
      }
      svg.classList.toggle('is-zoomed', zoomed);
      // 正悬浮着的提示、折线图的准线跟不上了，先收起来
      hideTip();
      for (const el of svg.querySelectorAll('.chart-cross, .chart-focus')) el.style.opacity = '0';
    }

    let frame = 0;
    function schedule() {
      if (frame) return;
      frame = requestAnimationFrame(function () { frame = 0; draw(); });
    }
    function clampOff() { state.off = Math.max(0, Math.min(span * (state.k - 1), state.off)); }
    // 以 cx（viewBox 横坐标）为中心放大到 k：那一点在屏幕上不动
    function zoomAround(k, cx) {
      k = Math.max(1, Math.min(kMax, k));
      const at = Math.max(x0, Math.min(x1, cx)) - x0;
      const u = (at + state.off) / state.k;
      state.off = u * k - at;
      state.k = k;
      clampOff();
      schedule();
    }
    let tween = 0;
    function zoomSmooth(k, cx) {
      cancelAnimationFrame(tween);
      if (calm) { zoomAround(k, cx); return; }
      const from = state.k, start = performance.now();
      const step = function (now) {
        const t = Math.min(1, (now - start) / 180);
        zoomAround(from + (k - from) * (1 - Math.pow(1 - t, 3)), cx);
        if (t < 1) tween = requestAnimationFrame(step);
      };
      tween = requestAnimationFrame(step);
    }
    const unit = function () {                // 屏幕上 1 像素是 viewBox 里的多少
      const box = svg.getBoundingClientRect();
      return svg.viewBox.baseVal.width / (box.width || 1);
    };
    const toView = function (clientX) { return (clientX - svg.getBoundingClientRect().left) * unit(); };
    const middle = function () { return x0 + span / 2; };

    svg.addEventListener('dblclick', function (event) {
      if (state.k > 1) { event.preventDefault(); zoomSmooth(1, middle()); }
    });

    // 滚轮：往上放大、往下缩小，以鼠标为中心（触控板捏合是带 Ctrl 的滚轮，一样）。缩到全貌还往下滚、
    // 放到最大还往上滚，就不拦着，照常滚页面——不会卡在图上滚不动。放大以后横着滑（触控板、Shift + 滚轮）：左右看
    svg.addEventListener('wheel', function (event) {
      const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 400 : 1;
      const dx = event.shiftKey && !event.deltaX ? event.deltaY : event.deltaX;
      const dy = event.shiftKey ? 0 : event.deltaY;
      if (Math.abs(dx) > Math.abs(dy)) {
        if (state.k <= 1) return;
        event.preventDefault();
        state.off += dx * scale * unit();
        clampOff();
        schedule();
        return;
      }
      if (!dy || (dy > 0 && state.k <= 1) || (dy < 0 && state.k >= kMax)) return;
      event.preventDefault();
      const speed = event.ctrlKey ? 0.01 : 0.0025;
      zoomAround(state.k * Math.exp(-dy * scale * speed), toView(event.clientX));
    }, { passive: false });

    // 按住拖：放大以后左右看。两根手指：捏合放大缩小
    const pointers = new Map();
    let drag = null, pinch = null, moved = false;
    svg.addEventListener('pointerdown', function (event) {
      if (event.pointerType === 'mouse' && event.button !== 0) return;
      pointers.set(event.pointerId, event.clientX);
      if (pointers.size === 2) {
        const xs = [...pointers.values()];
        pinch = { dist: Math.abs(xs[0] - xs[1]) || 1, k: state.k };
        drag = null;
      } else if (state.k > 1) {
        drag = { x: event.clientX, off: state.off };
        moved = false;
      }
    });
    window.addEventListener('pointermove', function (event) {
      if (!pointers.has(event.pointerId)) return;
      pointers.set(event.pointerId, event.clientX);
      if (pinch && pointers.size === 2) {
        const xs = [...pointers.values()];
        zoomAround(pinch.k * (Math.abs(xs[0] - xs[1]) || 1) / pinch.dist, toView((xs[0] + xs[1]) / 2));
        return;
      }
      if (!drag) return;
      const dx = event.clientX - drag.x;
      if (!moved && Math.abs(dx) < 4) return;
      if (!moved) { moved = true; svg.classList.add('is-panning'); hideTip(); }
      state.off = drag.off - dx * unit();
      clampOff();
      schedule();
    });
    const release = function (event) {
      pointers.delete(event.pointerId);
      if (pointers.size < 2) pinch = null;
      if (!pointers.size) { drag = null; svg.classList.remove('is-panning'); }
    };
    window.addEventListener('pointerup', release);
    window.addEventListener('pointercancel', release);
    // 拖过的那一下不算点击
    svg.addEventListener('click', function (event) {
      if (moved) { event.preventDefault(); event.stopPropagation(); moved = false; }
    }, true);
    draw();
  }

  for (const svg of document.querySelectorAll('svg[data-zoom]')) zoomable(svg);

  // ---- 气泡：悬浮的那个放大、其余变淡，提示里写名字、数和占比；
  //      还可以按住拖着走：拖着的泡跟着鼠标，挤到别的泡就把它们推开，
  //      松手后所有泡像弹簧一样弹回原位
  const SPRING = 0.07;     // 回原位的弹簧劲儿
  const DAMPING = 0.8;     // 每一帧速度留下多少，越小越快停
  const GAP = 3;           // 泡与泡之间至少留的缝（SVG 单位）

  for (const svg of document.querySelectorAll('.bubble-svg')) {
    const groups = [...svg.querySelectorAll('.bubble')];
    if (!groups.length) continue;
    const box = svg.viewBox.baseVal;
    const nodes = groups.map(function (g) {
      const c = g.querySelector('circle');
      const x = Number(c.getAttribute('cx'));
      const y = Number(c.getAttribute('cy'));
      return { g: g, layer: g.querySelector('.bubble-drag'), r: Number(c.getAttribute('r')),
               hx: x, hy: y, x: x, y: y, vx: 0, vy: 0 };
    });
    let dragged = null;
    let target = null;
    let grab = null;       // 按下时鼠标离圆心的偏移，拖动时保持不变
    let running = false;
    let moved = false;

    const toSvg = function (event) {
      const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(svg.getScreenCTM().inverse());
      return { x: point.x, y: point.y };
    };

    function frame() {
      for (const n of nodes) {
        if (n === dragged) { n.x = target.x; n.y = target.y; n.vx = n.vy = 0; continue; }
        n.vx = (n.vx + (n.hx - n.x) * SPRING) * DAMPING;
        n.vy = (n.vy + (n.hy - n.y) * SPRING) * DAMPING;
        n.x += n.vx;
        n.y += n.vy;
      }
      // 碰撞：重叠的两个泡沿连线推开；被拖着的那个不动，全推给对方
      for (let pass = 0; pass < 3; pass++) {
        for (let i = 0; i < nodes.length; i++) {
          for (let j = i + 1; j < nodes.length; j++) {
            const a = nodes[i], b = nodes[j];
            let dx = b.x - a.x, dy = b.y - a.y;
            let d = Math.hypot(dx, dy);
            const need = a.r + b.r + GAP;
            if (d >= need) continue;
            if (d < 0.01) { dx = 1; dy = 0; d = 1; }
            const push = need - d, ux = dx / d, uy = dy / d;
            if (a === dragged) { b.x += ux * push; b.y += uy * push; }
            else if (b === dragged) { a.x -= ux * push; a.y -= uy * push; }
            else {
              a.x -= ux * push / 2; a.y -= uy * push / 2;
              b.x += ux * push / 2; b.y += uy * push / 2;
            }
          }
        }
      }
      let busy = !!dragged;
      for (const n of nodes) {
        // 别被推出图外
        n.x = Math.max(box.x + n.r, Math.min(box.x + box.width - n.r, n.x));
        n.y = Math.max(box.y + n.r, Math.min(box.y + box.height - n.r, n.y));
        n.layer.setAttribute('transform', 'translate(' + (n.x - n.hx).toFixed(2) + ',' + (n.y - n.hy).toFixed(2) + ')');
        if (Math.abs(n.x - n.hx) > 0.1 || Math.abs(n.y - n.hy) > 0.1 || Math.abs(n.vx) > 0.02 || Math.abs(n.vy) > 0.02) busy = true;
      }
      if (busy) {
        requestAnimationFrame(frame);
      } else {
        running = false;
        for (const n of nodes) n.layer.removeAttribute('transform');
      }
    }
    const kick = function () { if (!running) { running = true; requestAnimationFrame(frame); } };

    for (const n of nodes) {
      const show = function () {
        if (dragged) return;
        svg.classList.add('is-hovering');
        for (const m of nodes) m.g.classList.toggle('is-hover', m === n);
        fillTip(n.g.dataset.name, n.g.dataset.value, []);
        const head = tipBox().querySelector('.tip-head');
        if (head) head.textContent = n.g.dataset.name + ' · 占 ' + n.g.dataset.share;
        placeTip(n.g.querySelector('circle').getBoundingClientRect());
      };
      const leave = function () {
        if (dragged) return;
        svg.classList.remove('is-hovering');
        n.g.classList.remove('is-hover');
        hideTip();
      };
      n.g.addEventListener('mouseenter', show);
      n.g.addEventListener('focus', show);
      n.g.addEventListener('mouseleave', leave);
      n.g.addEventListener('blur', leave);

      // 拖动时在整个窗口上听移动和松手：鼠标甩得快、离开了泡也不会断
      const onMove = function (event) {
        if (dragged !== n) return;
        const p = toSvg(event);
        target = { x: p.x - grab.x, y: p.y - grab.y };
        moved = true;
      };
      const release = function () {
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', release);
        window.removeEventListener('pointercancel', release);
        if (dragged !== n) return;
        dragged = null;
        svg.classList.remove('is-dragging', 'is-hovering');
        n.g.classList.remove('is-grabbed', 'is-hover');
        kick();
      };
      n.g.addEventListener('pointerdown', function (event) {
        if (event.button !== 0) return;
        event.preventDefault();
        const p = toSvg(event);
        dragged = n;
        moved = false;
        grab = { x: p.x - n.x, y: p.y - n.y };
        target = { x: n.x, y: n.y };
        svg.classList.add('is-dragging');
        n.g.classList.add('is-grabbed');
        hideTip();
        window.addEventListener('pointermove', onMove);
        window.addEventListener('pointerup', release);
        window.addEventListener('pointercancel', release);
        kick();
      });
      // 拖过之后的那次 click 不算点击
      n.g.addEventListener('click', function (event) { if (moved) event.preventDefault(); });
    }
  }
})();
