/* 横向时间线：客户页、账号页共用（可拖、可筛、从最近的那件开始看）。
 *
 * 页面上放一个 [data-timeline] 区块，里面有：
 *   script[data-tl-data]   JSON：{items: [...], today: "2026-10-10"}。名字、邮箱都是纯文本，这里一律用 textContent 放
 *   [data-tl-scroll] > [data-tl-track]   时间线画在这里（没开 JS 时是 hidden）
 *   [data-tl-fallback]     没开 JS 时的一列清单，脚本接手以后藏起来
 *   [data-tl-tools]        筛选（可选）：[data-tl-filter="cat"] 里的 .fchip，值在 data-value；
 *                          [data-tl-acct] 是按账号筛的下拉（app.js 的账号选择器），选了触发里面 select 的 change
 *   [data-tl-latest]       「回到最近」按钮（可选）
 *   template[data-icon]    卡片上的图标（服务端画好），按名字克隆
 * 点卡片：在区块上发一个 timeline:pick 事件（detail 是那一条），弹什么窗由页面自己定。
 *
 * 每一条：id、date、title、text、cat（筛选分组）、color（--k-* 的名字）、icon、auto（自动记的是实线框，手动的虚线框）、
 * who（卡片上那一行是谁：账号或者客户，{label, av: {text, css} 或 {img}}）、peer（替换时换上的那个，同样的结构）、
 * none（没有 who 时那一行写什么，比如「整个客户」）、keys（这件事涉及哪些账号的号码，按账号筛选用）。
 */
(function () {
  'use strict';
  const SLOT = 128, PAD = 146;
  const days = function (a, b) { return Math.round((Date.parse(b) - Date.parse(a)) / 86400000); };

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function person(who, arrow) {
    const line = el('span', 'tl-acct');
    if (arrow) line.append(el('span', 'tl-arrow', '→'));
    const face = who.av || {};
    let av;
    if (face.img) {
      av = el('img', 'mini-av mini-img');
      av.src = face.img;
      av.alt = '';
    } else {
      av = el('span', 'mini-av ' + (face.css || ''), face.text || '');
    }
    av.setAttribute('aria-hidden', 'true');
    line.append(av, el('span', 'tl-mail', who.label || ''));
    return line;
  }

  function mount(root) {
    const data = root.querySelector('script[data-tl-data]');
    const track = root.querySelector('[data-tl-track]');
    const scroller = root.querySelector('[data-tl-scroll]');
    if (!data || !track || !scroller) return;
    const payload = JSON.parse(data.textContent);
    const items = payload.items || [];
    const icons = {};
    for (const template of root.querySelectorAll('template[data-icon]')) icons[template.dataset.icon] = template;
    const picked = { cat: '', acct: '' };

    function card(item, x, side, order) {
      const box = el('div', 'tl-ev ' + side + ' k-' + item.color + (item.auto ? '' : ' is-hand'));
      box.style.left = x + 'px';
      box.style.setProperty('--i', Math.min(order, 14));
      box.append(el('span', 'tl-stem'), el('span', 'tl-dot'));
      const button = el('button', 'tl-card');
      button.type = 'button';
      button.dataset.ev = item.id;
      button.setAttribute('aria-label', item.title + '，' + item.date);
      const main = el('span', 'tl-main');
      const title = el('span', 'tl-title');
      const ico = el('span', 'tl-ico');
      if (icons[item.icon]) ico.append(icons[item.icon].content.cloneNode(true));
      title.append(ico, document.createTextNode(item.title));
      main.append(title);
      if (item.who) main.append(person(item.who));
      else if (item.none) main.append(el('span', 'tl-acct', item.none));
      if (item.peer) main.append(person(item.peer, true));
      if (item.text) main.append(el('span', 'tl-text', item.text));
      const day = new Date(item.date + 'T00:00:00');
      const date = el('span', 'tl-date');
      date.append(el('span', '', (day.getMonth() + 1) + '月'), el('b', '', String(day.getDate()).padStart(2, '0')),
                  el('span', '', String(day.getFullYear())));
      button.append(main, date);
      box.append(button);
      return box;
    }

    // 横轴分三段：第一件事之前、今天之后是虚线（还没开始 / 还没发生），中间是实线
    function axis(cls, from, to) {
      const line = el('div', 'tl-axis' + (cls ? ' ' + cls : ''));
      line.style.left = from + 'px';
      line.style.width = Math.max(0, to - from) + 'px';
      return line;
    }

    function render() {
      const shown = items.filter(function (item) {
        return (!picked.cat || item.cat === picked.cat) && (!picked.acct || (item.keys || []).indexOf(picked.acct) >= 0);
      });
      track.replaceChildren();
      if (!shown.length) {
        track.style.width = '100%';
        track.append(el('div', 'tl-axis is-before'), el('p', 'tl-empty', picked.acct ? '这个账号没有这一类的事。' : '没有这一类的事。'));
        return;
      }
      const firstX = PAD, todayX = PAD + (shown.length - 1) * SLOT + 90;
      const width = PAD * 2 + (shown.length - 1) * SLOT + 110;
      track.append(axis('is-before', 0, firstX), axis('is-flow', firstX, todayX), axis('is-after', todayX, width));
      // 进场从左往右：一开始滚在最右边（最近的那几件），按看得见的那几件从左到右一件件滑进来
      const visible = Math.max(1, Math.ceil((scroller.clientWidth || 900) / SLOT));
      const first = shown[0].date;
      const lead = el('span', 'tl-month', first.slice(0, 4) + ' 年 ' + Number(first.slice(5, 7)) + ' 月');
      lead.style.left = (PAD - 72) + 'px';
      track.append(lead);
      shown.forEach(function (item, i) {
        const x = PAD + i * SLOT;
        if (i > 0) {
          const prev = shown[i - 1];
          const mid = x - SLOT / 2;
          if (prev.date.slice(0, 7) !== item.date.slice(0, 7)) {
            const label = el('span', 'tl-month', (prev.date.slice(0, 4) !== item.date.slice(0, 4) ? item.date.slice(0, 4) + ' 年 ' : '') + Number(item.date.slice(5, 7)) + ' 月');
            label.style.left = mid + 'px';
            track.append(label);
          }
          const gap = days(prev.date, item.date);
          if (gap >= 4) { const g = el('span', 'tl-gap', gap + ' 天'); g.style.left = mid + 'px'; track.append(g); }
        }
        track.append(card(item, x, i % 2 ? 'down' : 'up', Math.max(0, i - (shown.length - visible))));
      });
      const last = shown[shown.length - 1];
      const tx = PAD + (shown.length - 1) * SLOT + 90;
      const today = el('span', 'tl-today', '今天 ' + payload.today.slice(5));
      today.style.left = tx + 'px';
      track.append(today);
      const since = days(last.date, payload.today);
      if (since >= 1) { const g = el('span', 'tl-gap', since + ' 天'); g.style.left = (tx - 44) + 'px'; track.append(g); }
      track.style.width = width + 'px';
    }
    function latest(smooth) { scroller.scrollTo({ left: scroller.scrollWidth, behavior: smooth ? 'smooth' : 'auto' }); }

    const fallback = root.querySelector('[data-tl-fallback]');
    if (fallback) fallback.hidden = true;
    scroller.hidden = false;
    const tools = root.querySelector('[data-tl-tools]');
    if (tools) tools.hidden = false;
    const latestButton = root.querySelector('[data-tl-latest]');
    if (latestButton) {
      latestButton.hidden = false;
      latestButton.addEventListener('click', function () { latest(true); });
    }
    render();
    latest(false);

    for (const group of root.querySelectorAll('[data-tl-filter]')) {
      const name = group.dataset.tlFilter;
      group.addEventListener('click', function (event) {
        const chip = event.target.closest('.fchip');
        if (!chip) return;
        picked[name] = chip.dataset.value || '';
        for (const other of group.querySelectorAll('.fchip')) other.setAttribute('aria-pressed', String(other === chip));
        render();
        latest(false);
      });
    }

    // 按账号筛：下拉的开合、搜索、键盘归 app.js；这里接 change，把按钮换成选中的那个（头像、邮箱、号码）
    const acct = root.querySelector('[data-tl-acct]');
    const acctSelect = acct && acct.querySelector('select');
    if (acctSelect) {
      acctSelect.addEventListener('change', function () {
        picked.acct = acctSelect.value;
        let chosen = null;
        for (const option of acct.querySelectorAll('.acct-opt')) {
          const on = option.dataset.value === acctSelect.value;
          option.setAttribute('aria-selected', String(on));
          if (on) chosen = option;
        }
        const face = chosen && picked.acct ? chosen.querySelector('.mini-av') : null;
        const av = acct.querySelector('[data-tl-acct-av]');
        av.className = face ? face.className : 'mini-av';
        av.textContent = face ? face.textContent : '';
        av.hidden = !face;
        const name = chosen && chosen.querySelector('.acct-opt-name');
        const sub = picked.acct && chosen ? chosen.querySelector('.acct-opt-sub') : null;
        acct.querySelector('[data-tl-acct-name]').textContent = name ? name.textContent : '全部账号';
        acct.querySelector('[data-tl-acct-sub]').textContent = sub ? sub.textContent : '';
        render();
        latest(false);
      });
    }

    // 拖着左右滚。拖过的那一下不算点卡片
    let start = null, moved = false;
    scroller.addEventListener('pointerdown', function (event) {
      if (event.button === 0) { start = { x: event.clientX, left: scroller.scrollLeft }; moved = false; }
    });
    window.addEventListener('pointermove', function (event) {
      if (!start) return;
      const dx = event.clientX - start.x;
      if (Math.abs(dx) > 4) { moved = true; scroller.classList.add('is-drag'); }
      if (moved) scroller.scrollLeft = start.left - dx;
    });
    window.addEventListener('pointerup', function () {
      start = null;
      setTimeout(function () { scroller.classList.remove('is-drag'); }, 0);
    });
    scroller.addEventListener('click', function (event) {
      if (moved) { event.preventDefault(); event.stopPropagation(); moved = false; }
    }, true);

    // 点卡片：交给页面（客户页能改日期、删掉；账号页只看）
    track.addEventListener('click', function (event) {
      const button = event.target.closest('[data-ev]');
      if (!button) return;
      const item = items.find(function (one) { return one.id === button.dataset.ev; });
      if (item) root.dispatchEvent(new CustomEvent('timeline:pick', { detail: item }));
    });
  }

  for (const root of document.querySelectorAll('[data-timeline]')) mount(root);

  // 「记一笔」弹窗（[data-note-form]，客户页、账号页都有）。选「标记风控」：会改生命周期，所以账号必选、
  // 只能选还能标的（选项上有 data-markable）；弹窗里写着标了以后会怎样，说明框的提示也换成风控的
  for (const form of document.querySelectorAll('[data-note-form]')) {
    const kind = form.querySelector('[data-note-kind]');
    const acct = form.querySelector('[data-note-acct]');
    const hint = form.querySelector('[data-note-risk]');
    const text = form.querySelector('textarea');
    if (!kind) continue;
    const usual = text ? text.placeholder : '';
    const riskOption = kind.querySelector('option[value="risk"]');
    if (acct && riskOption && ![].some.call(acct.options, function (o) { return o.dataset.markable; })) riskOption.disabled = true;
    const sync = function () {
      const risk = kind.value === 'risk';
      if (hint) hint.hidden = !risk;
      if (text) text.placeholder = risk ? '比如：收到 AWS 暂停通知' : usual;
      if (!acct) return;
      acct.required = risk;
      for (const option of acct.options) option.disabled = risk && !option.dataset.markable;
      if (acct.selectedOptions[0] && acct.selectedOptions[0].disabled) {
        const first = [].find.call(acct.options, function (o) { return !o.disabled; });
        acct.value = first ? first.value : '';
      }
    };
    kind.addEventListener('change', sync);
    sync();
  }
})();
