(() => {
  'use strict';
  const $ = (selector) => document.querySelector(selector);
  const modeInputs = [...document.querySelectorAll('input[name="demo-mode"]')];
  const steps = [...document.querySelectorAll('.path-step')];
  const playButton = $('#demo-play');
  const playLabel = playButton.querySelector('span');
  const message = $('#demo-message');
  const time = $('#demo-time');
  const state = $('#demo-state');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let currentMode = 'direct';
  let timers = [];

  const modes = {
    direct: {
      labels: ['持续申请', '核对结果', '确认加入'],
      captions: ['明确满员则继续等待', '结果不明，不再提交', '账户状态再次确认'],
      initial: '先完成账号、最低金额和余额检查，再开始持续申请。',
      events: [
        [0, '正在申请', '明确返回满员，按设置间隔继续尝试。此处仅模拟，不代表真实提交。'],
        [1, '抢位已暂停', '申请结果不明：暂停所有新的创建，只查询这次申请的结果。'],
        [1, '正在核对', '官方尚未显示已跟单，继续只读核对，不重复申请。'],
        [2, '模拟确认成功', '账户状态已确认加入：记录结果并提醒。全部目标完成后停止。']
      ]
    },
    slot: {
      labels: ['查询空位', '复核并申请', '核对结果'],
      captions: ['没有空位，继续等待', '重新核验金额和余额', '确认加入后提醒'],
      initial: '先查询公开名额；有空位时重新核验账号、最低金额和余额。',
      events: [
        [0, '正在查空位', '示例组合当前满员，继续查询。这里展示的是模拟状态。'],
        [1, '模拟发现空位', '发现空位，重新核验账号、金额和可用余额，再申请加入。'],
        [2, '抢位已暂停', '创建已发出：暂停新申请，只读核对跟单状态。'],
        [2, '模拟确认成功', '账户状态已确认加入，显示本次投入与模式，完成该目标。']
      ]
    },
    watch: {
      labels: ['查询名额', '发现空位', '仅作提醒'],
      captions: ['按设置间隔继续查询', '展示有效查询结果', '不创建真实跟单'],
      initial: '关闭真实资金，只查询公开名额；发现空位也不会自动提交跟单。',
      events: [
        [0, '正在查询', '只读查询示例组合名额，不使用真实资金创建跟单。'],
        [0, '继续等待', '示例组合暂时满员，按间隔继续查询。'],
        [1, '模拟发现空位', '示例组合出现空位，记录查询结果。'],
        [2, '仅监控，不提交', '发现空位并提示；真实资金未启用，不会自动申请加入。']
      ]
    }
  };

  function clearTimers() {
    timers.forEach(window.clearTimeout);
    timers = [];
  }
  function setState(text) {
    state.replaceChildren();
    const dot = document.createElement('span');
    dot.className = 'signal-dot';
    state.append(dot, document.createTextNode(text));
  }
  function setPath(index, complete = false) {
    steps.forEach((step, i) => {
      step.classList.toggle('is-current', i === index && !complete);
      step.classList.toggle('is-complete', i < index || (complete && i === index));
    });
  }
  function resetDemo() {
    clearTimers();
    const mode = modes[currentMode];
    steps.forEach((step, i) => {
      step.querySelector('.path-name').textContent = mode.labels[i];
      step.querySelector('small').textContent = mode.captions[i];
    });
    setPath(0);
    setState('等待演示');
    time.textContent = '准备就绪';
    message.textContent = mode.initial;
    playButton.disabled = false;
    playLabel.textContent = '播放运行过程';
    $('#demo').removeAttribute('aria-busy');
  }
  function showEvent(index) {
    const event = modes[currentMode].events[index];
    setPath(event[0], index === 3);
    setState(event[1]);
    time.textContent = `模拟事件 ${index + 1} / 4`;
    message.textContent = event[2];
    if (index === 3) {
      playButton.disabled = false;
      playLabel.textContent = '再看一次';
      $('#demo').removeAttribute('aria-busy');
    }
  }
  modeInputs.forEach((input) => input.addEventListener('change', () => {
    if (input.checked) {
      currentMode = input.value;
      resetDemo();
    }
  }));
  playButton.addEventListener('click', () => {
    clearTimers();
    playButton.disabled = true;
    playLabel.textContent = '演示进行中';
    // Keep the live message region available to assistive technology.
    if (reducedMotion.matches) {
      showEvent(3);
      return;
    }
    showEvent(0);
    [2200, 4700, 7600].forEach((delay, i) => {
      timers.push(window.setTimeout(() => showEvent(i + 1), delay));
    });
  });
  $('#demo-reset').addEventListener('click', resetDemo);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && playButton.disabled) resetDemo();
  });

  const menuButton = $('.menu-toggle');
  const nav = $('#main-nav');
  function closeMenu() {
    menuButton.setAttribute('aria-expanded', 'false');
    menuButton.setAttribute('aria-label', '打开导航');
    nav.classList.remove('is-open');
  }
  menuButton.addEventListener('click', () => {
    const open = menuButton.getAttribute('aria-expanded') !== 'true';
    menuButton.setAttribute('aria-expanded', String(open));
    menuButton.setAttribute('aria-label', open ? '关闭导航' : '打开导航');
    nav.classList.toggle('is-open', open);
  });
  nav.querySelectorAll('a').forEach((link) => link.addEventListener('click', closeMenu));
  document.addEventListener('click', (event) => {
    if (!nav.contains(event.target) && !menuButton.contains(event.target)) closeMenu();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && menuButton.getAttribute('aria-expanded') === 'true') {
      closeMenu();
      menuButton.focus();
    }
  });

  const dialog = $('#screenshot-dialog');
  $('#open-screenshot').addEventListener('click', () => {
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else window.open('assets/main-window.webp', '_blank', 'noopener');
  });
  $('#close-screenshot').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (event) => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
  });

  const checksum = $('#checksum-value');
  const copyButton = $('#copy-checksum');
  copyButton.addEventListener('click', async () => {
    const feedback = $('#copy-feedback');
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard-unavailable');
      await navigator.clipboard.writeText(checksum.textContent.trim());
      feedback.textContent = '已复制，可与下载文件的 SHA-256 比较。';
      checksum.hidden = true;
    } catch {
      checksum.hidden = false;
      feedback.textContent = '浏览器未允许复制。请选中下方校验值手动复制。';
    }
  });
})();
