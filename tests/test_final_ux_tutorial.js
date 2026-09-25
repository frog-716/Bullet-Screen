const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const EXPECTED_IDS = ['quick-start', 'dashboard', 'signals', 'troubleshooting'];
const bundles = Object.fromEntries(['bilibili', 'douyin'].map((platform) => [platform, {
  onboarding: require(`../${platform}/onboarding.js`),
  html: fs.readFileSync(path.join(root, platform, 'index.html'), 'utf8'),
  app: fs.readFileSync(path.join(root, platform, 'app.js'), 'utf8'),
  snapshot: fs.readFileSync(path.join(root, platform, 'snapshot_client.js'), 'utf8'),
  css: fs.readFileSync(path.join(root, platform, 'styles.css'), 'utf8'),
}]));

class FakeElement {
  constructor(id, doc, parent = null, selector = '') {
    this.id = id;
    this.doc = doc;
    this.parentElement = parent;
    this.selector = selector;
    this.listeners = new Map();
    this.attributes = new Map();
    this.dataset = {};
    this.style = {};
    this.classes = new Set();
    this.classList = {
      add: (...items) => items.forEach((item) => this.classes.add(item)),
      remove: (...items) => items.forEach((item) => this.classes.delete(item)),
      contains: (item) => this.classes.has(item),
      toggle: (item, force) => {
        const shouldAdd = force === undefined ? !this.classes.has(item) : Boolean(force);
        if (shouldAdd) this.classes.add(item); else this.classes.delete(item);
        return shouldAdd;
      },
    };
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.textContent = '';
    this.innerHTML = '';
    this.rect = { top: 180, left: 260, right: 360, bottom: 220, width: 100, height: 40 };
    this.scrollCount = 0;
    this.rectReadCount = 0;
    this.isConnected = true;
  }

  addEventListener(name, callback, options = false) {
    const listeners = this.listeners.get(name) || [];
    listeners.push({ callback, capture: options === true || Boolean(options?.capture) });
    this.listeners.set(name, listeners);
  }
  removeEventListener(name, callback, options = false) {
    const capture = options === true || Boolean(options?.capture);
    const listeners = this.listeners.get(name) || [];
    this.listeners.set(name, listeners.filter((item) => item.callback !== callback || item.capture !== capture));
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  removeAttribute(name) { this.attributes.delete(name); }
  hasAttribute(name) { return this.attributes.has(name); }
  focus() { this.doc.activeElement = this; }
  closest(selector) {
    if (selector === '[hidden]') {
      for (let node = this; node; node = node.parentElement) if (node.hidden) return node;
      return null;
    }
    if (selector === this.selector || selector === `#${this.id}`) return this;
    return null;
  }
  querySelectorAll(selector) {
    if (selector === '[data-tutorial-select]') return this.doc.categoryButtons;
    if (selector === '[data-tour-shade]') return this.doc.shades;
    if (selector.includes('button:not([disabled])')) return this.doc.routeDialogButtons;
    return [];
  }
  getBoundingClientRect() {
    this.rectReadCount += 1;
    if (this.id === 'tourTooltip') {
      const width = Number.parseFloat(this.style.width) || this.rect.width;
      const height = Number.parseFloat(this.style.maxHeight) || this.rect.height;
      const left = Number.parseFloat(this.style.left) || 0;
      const top = Number.parseFloat(this.style.top) || 0;
      return { ...this.rect, left, top, width, height, right: left + width, bottom: top + height };
    }
    return { ...this.rect };
  }
  getClientRects() { return this.hidden ? [] : [this.rect]; }
  scrollIntoView() {
    this.scrollCount += 1;
    const { innerWidth, innerHeight } = this.doc.defaultView;
    const width = Math.min(this.rect.width, innerWidth - 16);
    const height = Math.min(this.rect.height, innerHeight - 16);
    const left = Math.max(8, Math.min(innerWidth - width - 8, this.rect.left));
    const top = Math.max(8, Math.min(innerHeight - height - 8, this.rect.top));
    this.rect = { ...this.rect, left, top, right: left + width, bottom: top + height, width, height };
  }
  fire(name, event = {}) { return this.doc.dispatch(this, name, event); }
}

class FakeDocument {
  constructor({ width = 1280, height = 720 } = {}) {
    this.listeners = new Map();
    this.activeElement = null;
    this.elements = new Map();
    this.targets = new Map();
    this.evidenceOpen = false;
    const get = (id, parent = null, selector = '') => {
      if (!this.elements.has(id)) this.elements.set(id, new FakeElement(id, this, parent, selector));
      return this.elements.get(id);
    };
    this.body = get('body');
    this.modal = get('tutorialModal', this.body, '#tutorialModal');
    this.modal.hidden = true;
    this.dialog = get('tutorialDialog', this.modal, '#tutorialDialog');
    this.picker = get('tutorialPicker', this.dialog, '#tutorialPicker');
    this.openButton = get('tutorialButton', this.body, '#tutorialButton');
    this.closeButton = get('tutorialClose', this.dialog, '#tutorialClose');
    this.guidedTour = get('guidedTour', this.body, '#guidedTour');
    this.guidedTour.hidden = true;
    this.tooltip = get('tourTooltip', this.guidedTour, '#tourTooltip');
    this.tooltip.rect = { top: 0, left: 0, right: 320, bottom: 180, width: 320, height: 180 };
    this.title = get('tourStepTitle', this.tooltip, '#tourStepTitle');
    this.bodyText = get('tourStepBody', this.tooltip, '#tourStepBody');
    this.hint = get('tourStepHint', this.tooltip, '#tourStepHint');
    this.progress = get('tourProgress', this.tooltip, '#tourProgress');
    this.previous = get('tourPrevious', this.tooltip, '#tourPrevious');
    this.next = get('tourNext', this.tooltip, '#tourNext');
    this.exit = get('tourExit', this.tooltip, '#tourExit');
    this.shades = Array.from({ length: 4 }, (_, index) => get(`tourShade${index}`, this.guidedTour, '[data-tour-shade]'));
    this.shades.forEach((shade) => shade.hidden = true);
    this.categoryButtons = EXPECTED_IDS.map((id) => {
      const button = new FakeElement(`category-${id}`, this, this.picker, `[data-tutorial-select="${id}"]`);
      button.dataset.tutorialSelect = id;
      return button;
    });
    this.routeDialogButtons = [this.closeButton, ...this.categoryButtons];
    this.dialog.querySelectorAll = (selector) => selector === '[data-tutorial-select]' ? this.categoryButtons : [];
    this.tourCount = get('tourCount', this.tooltip, '#tourCount');
    this.routeButtons = new Map(this.categoryButtons.map((button) => [button.dataset.tutorialSelect, button]));
    this.defaultView = {
      innerWidth: width,
      innerHeight: height,
      listeners: new Map(),
      addEventListener: (name, callback) => {
        const items = this.defaultView.listeners.get(name) || [];
        items.push(callback);
        this.defaultView.listeners.set(name, items);
      },
      removeEventListener: (name, callback) => {
        this.defaultView.listeners.set(name, (this.defaultView.listeners.get(name) || []).filter((item) => item !== callback));
      },
      fire: (name) => (this.defaultView.listeners.get(name) || []).forEach((callback) => callback()),
    };
    this.documentElement = { clientWidth: width, clientHeight: height };
    for (const [id, selector] of [
      ['settingsButton', '#settingsButton'], ['roomInput', '#roomInput'], ['saveSettings', '#saveSettings'],
      ['settingsModal', '#settingsModal'], ['connectButton', '#connectButton'], ['truthPanel', '#truthPanel'],
      ['feedList', '#feedList'], ['metricGrid', '.metric-grid'], ['trendCard', '.trend-card'],
      ['rankingCard', '.ranking-card'], ['analysisCard', '.analysis-card'], ['keywordCard', '.keyword-card'],
      ['signalList', '#signalList'], ['diagnosticDetails', '#diagnosticDetails'], ['diagnosticSummary', '#diagnosticDetails > summary'],
      ['rangeSelect', '#rangeSelect'], ['recoveryAction', '[data-truth-action]'], ['signalCard', '.signal-row[data-signal-id]'],
      ['signalEvidence', '.signal-row[data-signal-id] .signal-evidence summary'],
      ['signalEvidenceOpen', '.signal-row[data-signal-id] .signal-evidence[open]'],
      ['signalFeedback', '.signal-row[data-signal-id] [data-feedback]'],
      ['signalEmpty', '#signalList .empty-state'],
    ]) this.targets.set(selector, get(id, this.body, selector));
    this.targets.get('#settingsModal').classList = this.targets.get('#settingsModal').classList;
    this.targets.get('#roomInput').value = '';
    this.targets.get('#settingsModal').open = false;
    this.targets.get('#settingsModal').setAttribute('aria-modal', 'true');
    this.targets.get('#settingsModal').setAttribute('aria-hidden', 'true');
    this.targets.get('#saveSettings').addEventListener('click', () => {
      const settings = this.targets.get('#settingsModal');
      settings.open = false;
      settings.classList.remove('open');
      settings.setAttribute('aria-hidden', 'true');
    });
    this.targets.get('#settingsButton').addEventListener('click', () => {
      const settings = this.targets.get('#settingsModal');
      settings.open = true;
      settings.classList.add('open');
      settings.setAttribute('aria-hidden', 'false');
    });
    this.targets.get('.signal-row[data-signal-id] .signal-evidence summary').addEventListener('click', () => {
      this.evidenceOpen = true;
    });
  }

  getElementById(id) { return this.elements.get(id) || null; }
  querySelector(selector) {
    if (selector.includes('[open]') && !this.evidenceOpen) return null;
    return this.targets.get(selector) || null;
  }
  querySelectorAll(selector) {
    if (selector === '[data-tutorial-select]') return this.categoryButtons;
    if (selector.includes('button:not([disabled])')) return this.routeDialogButtons;
    return [];
  }
  addEventListener(name, callback, options = false) {
    const listeners = this.listeners.get(name) || [];
    listeners.push({ callback, capture: options === true || Boolean(options?.capture) });
    this.listeners.set(name, listeners);
  }
  removeEventListener(name, callback, options = false) {
    const capture = options === true || Boolean(options?.capture);
    this.listeners.set(name, (this.listeners.get(name) || []).filter((item) => item.callback !== callback || item.capture !== capture));
  }
  dispatch(target, name, event = {}) {
    let defaultPrevented = false;
    const e = {
      type: name, target, currentTarget: null, key: '', shiftKey: false,
      preventDefault() { defaultPrevented = true; },
      stopPropagation() {},
      stopImmediatePropagation() { this.immediateStopped = true; },
      ...event,
    };
    e.preventDefault = () => { defaultPrevented = true; };
    e.defaultPrevented = false;
    for (const item of this.listeners.get(name) || []) if (item.capture) item.callback(e);
    for (const item of target.listeners.get(name) || []) item.callback(e);
    for (const item of this.listeners.get(name) || []) if (!item.capture) item.callback(e);
    e.defaultPrevented = defaultPrevented;
    return e;
  }
  createElement(tagName) { return new FakeElement(`${tagName}-${Math.random()}`, this); }
}

function sleep(ms = 5) { return new Promise((resolve) => setTimeout(resolve, ms)); }
function targetStep(route, selector) { return route.steps.find((step) => step.target === selector); }

for (const [platform, bundle] of Object.entries(bundles)) {
  const routes = bundle.onboarding.getTutorials?.();
  assert.ok(Array.isArray(routes), `${platform}: onboarding exposes tutorial routes`);
  assert.deepEqual(routes.map((route) => route.id), EXPECTED_IDS);
  for (const route of routes) {
    assert.ok(route.steps.length >= 1);
    const stepIds = route.steps.map((step) => step.id);
    assert.ok(stepIds.every((id) => typeof id === 'string' && id.length > 0), `${platform}/${route.id}: every step has a stable id`);
    assert.equal(new Set(stepIds).size, stepIds.length, `${platform}/${route.id}: step ids are unique within the route`);
    for (const step of route.steps) {
      assert.ok(step.target && step.title && step.body, `${platform}/${route.id}: each step names a real target and short copy`);
      assert.ok(['explain', 'action'].includes(step.type), `${platform}/${route.id}: step type is explicit`);
      assert.ok(step.placement, `${platform}/${route.id}: placement is explicit`);
      if (step.type === 'action') assert.ok(step.expectedAction, `${platform}/${route.id}: action waits for an expected user action`);
    }
    if (route.fallback) {
      assert.ok(route.fallback.steps.every((step) => typeof step.id === 'string' && step.id.length > 0), `${platform}/${route.id}: fallback steps have ids`);
    }
  }
  assert.match(bundle.html, /id="tutorialButton"[^>]*>\s*\?\s*使用教程/);
  assert.match(bundle.html, /id="tutorialModal"[^>]*hidden/);
  assert.match(bundle.html, /role="dialog"[^>]*aria-modal="true"/);
  assert.match(bundle.html, /id="guidedTour"[^>]*hidden/);
  assert.match(bundle.html, /id="tourTooltip"[^>]*role="group"/);
  assert.equal((bundle.html.match(/data-tour-shade/g) || []).length, 4, `${platform}: four cutout shades form a spotlight`);
  for (const id of ['tourStepTitle', 'tourStepBody', 'tourProgress', 'tourPrevious', 'tourNext', 'tourExit']) {
    assert.match(bundle.html, new RegExp(`id="${id}"`));
  }
  for (const selector of ['settingsButton', 'roomInput', 'saveSettings', 'connectButton', 'truthPanel', 'feedList', 'signalList', 'diagnosticDetails']) {
    assert.match(bundle.html, new RegExp(`id="${selector}"`), `${platform}: tour target #${selector} is a real dashboard element`);
  }
  assert.match(bundle.html, /class="metric-grid"/);
  assert.match(bundle.html, /id="rangeSelect"/);
  assert.match(bundle.html, /pointer-events:none/);
  assert.match(bundle.html, /\.guided-tour-target/);
  assert.match(bundle.html, /\.tour-tooltip/);
  assert.match(bundle.html, /\.tour-tooltip\{[^}]*overflow:auto/,
    `${platform}: tooltip body must remain scrollable in a short viewport`);
  assert.match(bundle.html, /\.tour-actions\{[^}]*position:sticky;[^}]*bottom:0/,
    `${platform}: navigation controls must stay reachable when the tooltip is height-limited`);
  assert.match(bundle.html, /id="truthHeadline"/);
  assert.match(bundle.html, /id="truthRoom"/);
  assert.match(bundle.html, /id="truthWindow"/);
  assert.match(bundle.html, /id="truthCoverage"/);
  assert.match(bundle.html, /id="truthAsOf"/);
  assert.match(bundle.html, /id="lastReliableLabel"/);
  assert.match(bundle.html, /id="diagnosticDetails"/);
  assert.match(bundle.app, /演示数据|Demo/);
  assert.match(bundle.snapshot, /数据连续可靠|采集正常/);
  assert.match(bundle.snapshot, /数据完整|缺口|不完整/);
}

const biliRoutes = bundles.bilibili.onboarding.getTutorials();
const douyinRoutes = bundles.douyin.onboarding.getTutorials();
const biliQuick = biliRoutes.find((route) => route.id === 'quick-start');
const dyQuick = douyinRoutes.find((route) => route.id === 'quick-start');
assert.equal(targetStep(biliQuick, '#settingsButton')?.type, 'action');
assert.equal(targetStep(biliQuick, '#roomInput')?.type, 'action');
assert.equal(targetStep(biliQuick, '#saveSettings')?.expectedAction?.event, 'click');
assert.ok(targetStep(biliQuick, '#connectButton'));
assert.ok(targetStep(biliQuick, '#truthPanel'));
assert.equal(targetStep(dyQuick, '#settingsButton')?.type, 'action');
assert.match(dyQuick.steps.map((step) => step.body).join(' '), /页面能打开不代表.*互动数据/);
assert.ok(targetStep(dyQuick, '#truthPanel'));

const biliDashboard = biliRoutes.find((route) => route.id === 'dashboard');
const dyDashboard = douyinRoutes.find((route) => route.id === 'dashboard');
assert.ok(targetStep(biliDashboard, '#truthPanel'));
assert.ok(targetStep(biliDashboard, '#feedList'));
assert.ok(targetStep(biliDashboard, '.metric-grid'));
assert.ok(targetStep(biliDashboard, '.trend-card'));
assert.ok(targetStep(biliDashboard, '.ranking-card') || targetStep(biliDashboard, '.keyword-card'));
assert.ok(targetStep(biliDashboard, '#signalList'));
assert.ok(targetStep(dyDashboard, '#rangeSelect')?.expectedAction);
assert.ok(targetStep(dyDashboard, '.analysis-card'));

for (const routes of [biliRoutes, douyinRoutes]) {
  const signalRoute = routes.find((route) => route.id === 'signals');
  assert.ok(targetStep(signalRoute, '.signal-row[data-signal-id]'));
  assert.ok(targetStep(signalRoute, '.signal-row[data-signal-id] .signal-evidence summary')?.expectedAction);
  assert.ok(targetStep(signalRoute, '.signal-row[data-signal-id] .signal-evidence[open]'));
  assert.ok(targetStep(signalRoute, '.signal-row[data-signal-id] [data-feedback]'));
  assert.match(signalRoute.fallback.steps[0].body, /没有.*Signal|没有.*信号/);
  assert.match(signalRoute.steps.map((step) => step.body).join(' '), /不是事实|不是购买概率/);
}
const biliTrouble = biliRoutes.find((route) => route.id === 'troubleshooting');
assert.ok(targetStep(biliTrouble, '#truthPanel'));
assert.ok(targetStep(biliTrouble, '#truthActions'));
assert.ok(targetStep(biliTrouble, '#diagnosticDetails > summary'));
assert.match(douyinRoutes.find((route) => route.id === 'troubleshooting').steps.map((step) => step.body).join(' '), /等待互动数据.*暂时无法读取互动.*最近暂无新互动.*采集可能中断/);

async function exerciseConnectionActions(platform) {
  const doc = new FakeDocument();
  assert.equal(bundles[platform].onboarding.bindTutorialUI(doc), true);
  doc.openButton.fire('click');
  doc.categoryButtons[0].fire('click');
  assert.equal(doc.modal.hidden, true, 'route choice closes the selector modal');
  assert.equal(doc.guidedTour.hidden, false, 'guided tour appears on the live dashboard');
  assert.equal(doc.title.textContent, '打开连接设置');
  const settingsButton = doc.querySelector('#settingsButton');
  settingsButton.fire('click');
  await sleep();
  assert.equal(doc.targets.get('#settingsModal').open, true, 'the real settings button opens the real settings modal');
  assert.equal(doc.targets.get('#settingsModal').getAttribute('aria-modal'), 'false', 'the real settings dialog yields modal semantics while the tour controls are active');
  assert.equal(doc.title.textContent, '填写直播间');
  const roomInput = doc.querySelector('#roomInput');
  roomInput.value = 'not-a-room';
  roomInput.fire('change');
  await sleep();
  assert.equal(doc.title.textContent, '填写直播间', 'invalid room input does not advance the action step');
  roomInput.value = platform === 'bilibili' ? '123456' : 'https://live.douyin.com/123456';
  roomInput.fire('change');
  await sleep();
  assert.equal(doc.title.textContent, '保存直播间设置');
  doc.querySelector('#saveSettings').fire('click');
  await sleep();
  assert.equal(doc.targets.get('#settingsModal').open, false);
  assert.equal(doc.targets.get('#settingsModal').getAttribute('aria-modal'), 'true', 'the settings dialog restores its modal semantics when it closes');
  assert.equal(doc.title.textContent, platform === 'bilibili' ? '开始采集' : '开始连接');
  assert.equal(doc.querySelector('#connectButton').classList.contains('guided-tour-target'), true);
  doc.exit.fire('click');
  await sleep();
  assert.equal(doc.guidedTour.hidden, true);
  assert.equal(doc.openButton.focused || doc.activeElement === doc.openButton, true, 'exit returns focus to tutorial entry');
  assert.equal((doc.listeners.get('click') || []).length, 0, 'exit removes the action listener');
  assert.equal((doc.defaultView.listeners.get('resize') || []).length, 0, 'exit removes resize listener');
}

async function exerciseFallbackAndActionSkip() {
  const doc = new FakeDocument({ width: 540, height: 360 });
  doc.targets.delete('.signal-row[data-signal-id]');
  assert.equal(bundles.douyin.onboarding.bindTutorialUI(doc), true);
  doc.openButton.fire('click');
  doc.categoryButtons[2].fire('click');
  assert.equal(doc.title.textContent, '当前没有满足条件的信号');
  assert.match(doc.bodyText.textContent, /没有.*Signal|没有.*信号/);
  assert.equal(doc.querySelector('#signalList .empty-state').classList.contains('guided-tour-target'), true, 'no-signal fallback highlights the real empty state');
  doc.exit.fire('click');
  doc.openButton.fire('click');
  doc.categoryButtons[0].fire('click');
  assert.match(doc.title.textContent, /打开连接设置/);
  doc.next.fire('click'); // Action can always be skipped manually.
  assert.match(doc.title.textContent, /填写直播间/);
  doc.exit.fire('click');
}

async function exercisePickerReplacesActiveTour(platform) {
  const doc = new FakeDocument();
  assert.equal(bundles[platform].onboarding.bindTutorialUI(doc), true);
  doc.openButton.fire('click');
  doc.categoryButtons[0].fire('click');
  assert.equal(doc.guidedTour.hidden, false);

  // Reopening the route chooser from the dashboard must replace, not cover, a running tour.
  doc.openButton.fire('click');
  assert.equal(doc.modal.hidden, false, `${platform}: route picker opens`);
  assert.equal(doc.guidedTour.hidden, true, `${platform}: the previous tour is closed before the picker opens`);
  assert.equal(doc.querySelector('#settingsButton').classList.contains('guided-tour-target'), false);
  assert.equal((doc.listeners.get('click') || []).length, 0, `${platform}: old action listeners are removed`);

  doc.categoryButtons[2].fire('click');
  assert.equal(doc.modal.hidden, true, `${platform}: choosing another route closes the picker`);
  assert.equal(doc.guidedTour.hidden, false, `${platform}: only the newly selected tour remains open`);
  assert.equal(doc.title.textContent, '先看真实信号');
  doc.exit.fire('click');
}

async function exerciseRangeAndEvidenceActions() {
  const dy = new FakeDocument();
  bundles.douyin.onboarding.bindTutorialUI(dy);
  dy.openButton.fire('click');
  dy.categoryButtons[1].fire('click');
  for (let index = 0; index < 5; index += 1) dy.next.fire('click');
  assert.equal(dy.title.textContent, '切换统计范围');
  dy.querySelector('#rangeSelect').value = '10s';
  dy.querySelector('#rangeSelect').fire('change');
  await sleep();
  assert.equal(dy.title.textContent, '规则信号', 'changing the real range control advances the guided route');
  dy.exit.fire('click');

  const bili = new FakeDocument();
  bundles.bilibili.onboarding.bindTutorialUI(bili);
  bili.openButton.fire('click');
  bili.categoryButtons[2].fire('click');
  assert.equal(bili.title.textContent, '先看真实信号');
  bili.next.fire('click');
  assert.equal(bili.title.textContent, '打开证据');
  bili.querySelector('.signal-row[data-signal-id] .signal-evidence summary').fire('click');
  await sleep();
  assert.equal(bili.title.textContent, '核对证据', 'opening the real evidence disclosure advances to the expanded evidence');
  bili.next.fire('click');
  assert.equal(bili.title.textContent, '留下反馈');
  bili.exit.fire('click');
}

async function exercisePreviousEscapeAndCleanup() {
  const doc = new FakeDocument();
  bundles.douyin.onboarding.bindTutorialUI(doc);
  doc.openButton.fire('click');
  doc.categoryButtons[1].fire('click');
  doc.next.fire('click');
  assert.equal(doc.title.textContent, '实时互动');
  doc.previous.fire('click');
  assert.equal(doc.title.textContent, '先确认数据状态');
  assert.equal(doc.querySelector('#truthPanel').classList.contains('guided-tour-target'), true);
  const escape = doc.dispatch(doc.tooltip, 'keydown', { key: 'Escape' });
  assert.equal(escape.defaultPrevented, true);
  assert.equal(escape.immediateStopped, true, 'Escape exits the tour without also closing an active settings modal');
  assert.equal(doc.guidedTour.hidden, true);
  assert.equal(doc.openButton.focused || doc.activeElement === doc.openButton, true);
  assert.equal(doc.querySelector('#truthPanel').classList.contains('guided-tour-target'), false);
  assert.equal(doc.querySelector('#truthPanel').getAttribute('aria-describedby'), null);
  assert.equal((doc.listeners.get('click') || []).length, 0);
  assert.equal((doc.listeners.get('change') || []).length, 0);
  assert.equal((doc.listeners.get('keydown') || []).length, 0);
  assert.equal((doc.listeners.get('scroll') || []).length, 0);
}

async function exerciseMissingTargetAndPositioning() {
  const doc = new FakeDocument({ width: 520, height: 330 });
  assert.equal(bundles.bilibili.onboarding.bindTutorialUI(doc), true);
  doc.openButton.fire('click');
  doc.categoryButtons[1].fire('click');
  const currentTitle = doc.title.textContent;
  doc.targets.delete('#truthPanel');
  doc.defaultView.fire('resize');
  await sleep();
  assert.match(doc.bodyText.textContent, /^当前这一步暂时不可用/);
  assert.equal(doc.next.hidden, false);
  doc.targets.set('#truthPanel', new FakeElement('truthPanel-restored', doc, doc.body, '#truthPanel'));
  doc.defaultView.fire('resize');
  await sleep();
  assert.equal(doc.bodyText.textContent, targetStep(biliDashboard, '#truthPanel').body, 'a target that returns restores the route copy');
  doc.next.fire('click');
  assert.notEqual(doc.title.textContent, currentTitle);

  const target = doc.querySelector('#feedList');
  target.rect = { top: -40, left: 430, right: 690, bottom: 170, width: 260, height: 210 };
  target.scrollCount = 0;
  doc.defaultView.fire('resize');
  await sleep();
  assert.ok(target.scrollCount > 0, 'off-screen target scrolls into view');
  assert.ok(target.rect.left >= 8 && target.rect.right <= doc.defaultView.innerWidth - 8);
  assert.ok(target.rect.top >= 8 && target.rect.bottom <= doc.defaultView.innerHeight - 8);
  assert.ok(Number.parseFloat(doc.tooltip.style.left) >= 8);
  const effectiveTip = doc.tooltip.getBoundingClientRect();
  assert.ok(effectiveTip.left >= 8 && effectiveTip.right <= doc.defaultView.innerWidth - 8);
  assert.ok(Number.parseFloat(doc.tooltip.style.top) >= 8);
  assert.ok(effectiveTip.top >= 8 && effectiveTip.bottom <= doc.defaultView.innerHeight - 8);
  const readsBeforeResize = target.rectReadCount;
  doc.defaultView.innerWidth = 360;
  doc.defaultView.innerHeight = 240;
  doc.documentElement.clientWidth = 360;
  doc.documentElement.clientHeight = 240;
  doc.defaultView.fire('resize');
  await sleep();
  const zoomTip = doc.tooltip.getBoundingClientRect();
  assert.ok(zoomTip.left >= 8 && zoomTip.right <= 352, 'tour tooltip stays inside a narrow 200%-zoom viewport');
  assert.ok(zoomTip.top >= 8 && zoomTip.bottom <= 232, 'tour tooltip remains reachable inside a short viewport');
  assert.ok(target.rectReadCount > readsBeforeResize, 'resize recalculates the target and tooltip placement');
  assert.equal(doc.defaultView.scrollX || 0, 0, 'tour positioning does not create persistent horizontal page scroll');
  doc.defaultView.innerWidth = 1280;
  doc.defaultView.innerHeight = 720;
  doc.documentElement.clientWidth = 1280;
  doc.documentElement.clientHeight = 720;
  target.rect = { top: 250, left: 400, right: 660, bottom: 460, width: 260, height: 210 };
  doc.defaultView.fire('resize');
  await sleep();
  assert.equal(doc.tooltip.style.width || '', '', 'a wider viewport restores the tooltip natural width');
  assert.equal(doc.tooltip.style.maxHeight || '', '', 'a taller viewport restores the tooltip natural height');
  target.fire('click');
  assert.ok(doc.targets.get('#feedList').listeners.get('click') === undefined || doc.targets.get('#feedList').listeners.get('click').length === 0, 'the tour overlay does not add a click blocker to the target');
  doc.exit.fire('click');
}

async function exerciseTallTargetKeepsTourControlsReachable(platform) {
  const doc = new FakeDocument({ width: 659, height: 387 });
  bundles[platform].onboarding.bindTutorialUI(doc);
  doc.targets.get('#truthPanel').rect = {
    top: 100, left: 0, right: 659, bottom: 387, width: 659, height: 287,
  };
  doc.openButton.fire('click');
  doc.categoryButtons[1].fire('click');
  await sleep();
  const top = Number.parseFloat(doc.tooltip.style.top);
  const maxHeight = Number.parseFloat(doc.tooltip.style.maxHeight);
  assert.ok(maxHeight >= 371,
    `${platform}: when a highlighted target leaves too little room, the tour falls back to a full-height scrollable panel`);
  assert.ok(top >= 8 && top + maxHeight <= 379,
    `${platform}: fallback tooltip and its exit/next controls stay within the viewport`);
  doc.exit.fire('click');
}

async function exerciseDouyinMissingTargetRecovery() {
  const doc = new FakeDocument();
  bundles.douyin.onboarding.bindTutorialUI(doc);
  doc.targets.delete('#settingsButton');
  doc.openButton.fire('click');
  doc.categoryButtons[0].fire('click');
  assert.match(doc.bodyText.textContent, /^当前这一步暂时不可用/);
  const settingsButton = new FakeElement('settingsButton-restored', doc, doc.body, '#settingsButton');
  doc.targets.set('#settingsButton', settingsButton);
  doc.defaultView.fire('resize');
  await sleep();
  assert.equal(doc.bodyText.textContent, '先打开连接设置。', 'Douyin restores route instructions when the target becomes available');
  assert.equal(settingsButton.classList.contains('guided-tour-target'), true);
  doc.exit.fire('click');
}

async function exerciseExitWhileSettingsIsOpen(platform) {
  const doc = new FakeDocument();
  bundles[platform].onboarding.bindTutorialUI(doc);
  doc.openButton.fire('click');
  doc.categoryButtons[0].fire('click');
  doc.querySelector('#settingsButton').fire('click');
  await sleep();
  const escape = doc.dispatch(doc.querySelector('#roomInput'), 'keydown', { key: 'Escape' });
  assert.equal(escape.immediateStopped, true);
  assert.equal(doc.guidedTour.hidden, true);
  assert.equal(doc.targets.get('#settingsModal').open, true, 'exiting the tour does not close the real settings dialog');
  assert.equal(doc.targets.get('#settingsModal').getAttribute('aria-modal'), 'true', 'the settings modal regains exclusive modal semantics');
  assert.equal(doc.activeElement, doc.querySelector('#roomInput'), 'focus remains inside the still-open real settings dialog');
  assert.equal(doc.querySelector('#settingsButton').classList.contains('guided-tour-target'), false);
}

(async () => {
  await exerciseConnectionActions('bilibili');
  await exerciseConnectionActions('douyin');
  await exerciseFallbackAndActionSkip();
  await exercisePickerReplacesActiveTour('bilibili');
  await exercisePickerReplacesActiveTour('douyin');
  await exerciseRangeAndEvidenceActions();
  await exercisePreviousEscapeAndCleanup();
  await exerciseMissingTargetAndPositioning();
  await exerciseTallTargetKeepsTourControlsReachable('bilibili');
  await exerciseTallTargetKeepsTourControlsReachable('douyin');
  await exerciseDouyinMissingTargetRecovery();
  await exerciseExitWhileSettingsIsOpen('bilibili');
  await exerciseExitWhileSettingsIsOpen('douyin');
})().then(() => {
  assert.doesNotMatch(bundles.bilibili.html, /数量与平台原始值分开记录|连接并收到礼物或 SC 后自动记账/);
  assert.match(bundles.douyin.html, /LIVE INTELLIGENCE \/ MVP/);
  assert.match(bundles.douyin.html, /EVENT STREAM/);
  assert.match(bundles.bilibili.html, /弹幕 \/ 分/);
  assert.match(bundles.bilibili.html, /币种未知时不估算金额/);
  assert.match(bundles.douyin.html, /评论事件 \/ 分/);
  assert.match(bundles.douyin.html, /协议不可用时才使用页面约值/);
  assert.match(bundles.douyin.app, /页面已打开|互动数据/);
  const limitations = fs.readFileSync(path.join(root, 'docs/KNOWN-LIMITATIONS.md'), 'utf8');
  assert.match(limitations, /稳定基线：`bullet-screen-v1-final`/);
  assert.match(limitations, /200% 页面缩放/);
  const readme = fs.readFileSync(path.join(root, 'README.md'), 'utf8');
  assert.match(readme, /已知限制与后续计划[\s\S]{0,100}docs\/KNOWN-LIMITATIONS\.md/);
  console.log('Guided tutorial tour contracts passed.');
}).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
