/* 用户管理（templates/people.html，和管理员点开的资料页 profile.html）：弹窗在 _people_macros.html。
 *  · 新建、重置密码：fetch 发（回 JSON），回来的密码填进「密码」弹窗，只显示这一次；错了写在原来的弹窗里
 *  · 密码怎么定：系统生成（默认）/ 自己填（选了才出现那个框）
 *  · 「密码」弹窗关了（「我记下了」、按 Esc）：把密码从页面上抹掉，刷新这一页
 *  · 删除：把是哪个用户填进确认弹窗，提交是普通表单
 * 所有从服务器拿到的字一律用 textContent 放进去。 */
(function () {
  'use strict';
  const shown = document.getElementById('dlg-user-password');
  if (!shown) return;

  function fill(dialog, name, text) {
    const target = dialog.querySelector('[data-fill="' + name + '"]');
    if (target) target.textContent = text;
  }
  function syncMode(form) {
    const custom = form.querySelector('[data-pw-mode][value="custom"]');
    const box = form.querySelector('[data-pw-custom]');
    if (!custom || !box) return;
    box.hidden = !custom.checked;
    box.querySelector('input').required = custom.checked;
  }
  function open(dialog) {
    const form = dialog.querySelector('form');
    if (form) { form.reset(); syncMode(form); }
    const error = dialog.querySelector('[data-user-error]');
    if (error) { error.hidden = true; error.textContent = ''; }
    dialog.showModal();
    const first = dialog.querySelector('[data-autofocus]');
    if (first) first.focus();
  }

  document.addEventListener('click', function (event) {
    if (!(event.target instanceof Element)) return;
    const opener = event.target.closest('[data-open="dlg-user-create"]');
    if (opener) { open(document.getElementById('dlg-user-create')); return; }
    // 重置密码、删除：表单提交到这个用户的网址，标题旁边写是谁
    const pick = event.target.closest('[data-user-reset], [data-user-delete]');
    if (pick) {
      const reset = pick.hasAttribute('data-user-reset');
      const dialog = document.getElementById(reset ? 'dlg-user-reset' : 'dlg-user-delete');
      open(dialog);
      dialog.querySelector('form').action = pick.getAttribute(reset ? 'data-user-reset' : 'data-user-delete');
      fill(dialog, 'name', pick.dataset.name);
      return;
    }
    const closer = event.target.closest('[data-close]');
    if (closer && closer.closest('dialog')) closer.closest('dialog').close();
  });
  for (const dialog of document.querySelectorAll('dialog.modal')) {
    if (dialog === shown) continue;
    dialog.addEventListener('click', function (event) { if (event.target === dialog) dialog.close(); });
  }
  document.addEventListener('change', function (event) {
    if (event.target.matches && event.target.matches('[data-pw-mode]')) {
      const form = event.target.closest('form');
      syncMode(form);
      if (event.target.value === 'custom') form.querySelector('[data-pw-custom] input').focus();
    }
  });

  // ---- 新建、重置：fetch 发，回来的密码填进「密码」弹窗
  for (const form of document.querySelectorAll('form[data-user-form]')) {
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      const dialog = form.closest('dialog');
      const error = dialog.querySelector('[data-user-error]');
      const button = form.querySelector('[type="submit"]');
      button.disabled = true;
      fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        headers: { 'X-Requested-With': 'fetch' },
        credentials: 'same-origin'
      })
        .then(function (response) { return response.json(); })
        .then(function (result) {
          if (!result.ok) {
            error.textContent = result.message || '没成功，再试一次。';
            error.hidden = false;
            return;
          }
          dialog.close();
          form.reset();
          fill(shown, 'title', dialog.id === 'dlg-user-create' ? '建好了' : '新密码');
          fill(shown, 'name', result.name);
          fill(shown, 'password', result.password);
          shown.querySelector('[data-copy-label]').textContent = '复制';
          shown.showModal();
        })
        .catch(function () {
          error.textContent = '没成功：页面可能已经过期，刷新后再试。';
          error.hidden = false;
        })
        .finally(function () { button.disabled = false; });
    });
  }

  // ---- 「密码」弹窗：关了（怎么关的都算）把密码抹掉、刷新这一页
  shown.addEventListener('close', function () {
    fill(shown, 'password', '');
    window.location.reload();
  });
  shown.querySelector('[data-done]').addEventListener('click', function () { shown.close(); });
  shown.querySelector('[data-copy]').addEventListener('click', function () {
    const code = shown.querySelector('[data-fill="password"]');
    const label = shown.querySelector('[data-copy-label]');
    const select = function () {
      // 剪贴板用不了（不是 HTTPS、浏览器不让）：选中密码再试一次老办法，还不行就让人自己按 Ctrl+C
      const range = document.createRange();
      range.selectNodeContents(code);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      let copied = false;
      try { copied = document.execCommand('copy'); } catch (error) { copied = false; }
      label.textContent = copied ? '已复制' : '已选中，按 Ctrl+C';
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(code.textContent).then(function () { label.textContent = '已复制'; }, select);
    } else {
      select();
    }
  });
})();
