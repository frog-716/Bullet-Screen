(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BulletScreenOnboarding = api;
  if (root && root.document) api.bindTutorialUI(root.document);
})(typeof globalThis === "object" ? globalThis : this, function () {
  const TUTORIALS = [
    {
      id: "quick-start", title: "第一次连接直播间", steps: [
        { id: "open-settings", target: "#settingsButton", title: "打开连接设置", body: "先打开连接设置。", type: "action", placement: "left", expectedAction: { event: "click", selector: "#settingsButton" } },
        { id: "enter-room", target: "#roomInput", title: "填写直播间", body: "把 Douyin 公开直播间链接或房间号填在这里。", type: "action", placement: "right", expectedAction: { event: "change", selector: "#roomInput", validate: "room" } },
        { id: "save-room", target: "#saveSettings", title: "保存直播间设置", body: "填写完成后，点这里保存。", type: "action", placement: "top", expectedAction: { event: "click", selector: "#saveSettings" } },
        { id: "start-capture", target: "#connectButton", title: "开始连接", body: "点这里打开直播页面；页面能打开不代表互动数据一定可用。", type: "action", placement: "bottom", expectedAction: { event: "click", selector: "#connectButton" } },
        { id: "check-status", target: "#truthPanel", title: "确认互动读取状态", body: "这里会告诉你互动数据是否真的可用。并非所有公开房间都能读取互动。", type: "explain", placement: "bottom" },
      ],
    },
    {
      id: "dashboard", title: "看懂这个看板", steps: [
        { id: "status", target: "#truthPanel", title: "先确认数据状态", body: "先看这里，确认当前页面是否真的读到了互动，以及数据是否可靠。", type: "explain", placement: "bottom" },
        { id: "feed", target: "#feedList", title: "实时互动", body: "这里显示本次连接收到的评论、点赞、礼物和其他互动。", type: "explain", placement: "right" },
        { id: "metrics", target: ".metric-grid", title: "核心指标", body: "这里查看在线人数、评论、点赞和互动人数；没有可靠数据时会明确显示未知。", type: "explain", placement: "bottom" },
        { id: "trend", target: ".trend-card", title: "互动趋势", body: "这里对照互动速度和在线人数的变化。", type: "explain", placement: "top" },
        { id: "analysis", target: ".analysis-card", title: "主题和问题", body: "这里整理最近的热门主题与高频问题。", type: "explain", placement: "left" },
        { id: "range", target: "#rangeSelect", title: "切换统计范围", body: "这里可以切换 10 秒、60 秒和 5 分钟统计范围。", type: "action", placement: "bottom", expectedAction: { event: "change", selector: "#rangeSelect" } },
        { id: "signals", target: "#signalList", title: "规则信号", body: "这里显示满足规则条件的提示；没有信号不代表页面或采集出错。", type: "explain", placement: "top" },
      ],
    },
    {
      id: "signals", title: "信号和证据怎么用", steps: [
        { id: "inspect-signal", target: ".signal-row[data-signal-id]", title: "先看真实信号", body: "这是保守规则发现的提示，不是事实预测，也不是购买概率。", type: "explain", placement: "top" },
        { id: "open-evidence", target: ".signal-row[data-signal-id] .signal-evidence summary", title: "打开证据", body: "点这里查看哪些真实互动事件支持这个提示。", type: "action", placement: "top", expectedAction: { event: "click", selector: ".signal-row[data-signal-id] .signal-evidence summary" } },
        { id: "verify-evidence", target: ".signal-row[data-signal-id] .signal-evidence[open]", title: "核对证据", body: "这里列出支持信号的事件时间、类型和必要摘要。", type: "explain", placement: "top" },
        { id: "provide-feedback", target: ".signal-row[data-signal-id] [data-feedback]", title: "留下反馈", body: "你可以标记有用或误报，也可以补充备注。", type: "explain", placement: "top" },
      ],
      fallback: { steps: [
        { id: "no-signal", target: "#signalList .empty-state", title: "当前没有满足条件的信号", body: "现在没有符合规则门槛的 Signal。出现后可在这里查看证据和反馈。", type: "explain", placement: "top" },
      ] },
    },
    {
      id: "troubleshooting", title: "出问题怎么办", steps: [
        { id: "status", target: "#truthPanel", title: "先看当前状态", body: "出问题时先看这里，不要先猜。页面已打开不等于互动已读取。", type: "explain", placement: "bottom" },
        { id: "recovery-actions", target: "#truthActions", title: "按提示恢复", body: "如果这里出现重新连接或修改房间等按钮，可以按提示处理；没有按钮时可跳过。", type: "explain", placement: "bottom" },
        { id: "status-meanings", target: "#truthPanel", title: "分清几种状态", body: "等待互动数据表示还在确认；暂时无法读取互动时可检查登录并重连；最近暂无新互动不等于失败；采集可能中断时请重新连接。", type: "explain", placement: "bottom" },
        { id: "diagnostics", target: "#diagnosticDetails > summary", title: "需要时查看详情", body: "普通提示解决不了时，再展开这里查看技术诊断。", type: "explain", placement: "bottom" },
      ],
    },
  ];

  function normalizeRoomInput(value) {
    const raw = String(value || "").trim();
    if (/^\d+$/.test(raw)) return raw;
    try {
      const url = new URL(raw);
      if (!/^https?:$/.test(url.protocol) || url.hostname.toLowerCase() !== "live.douyin.com") return "";
      const room = url.pathname.split("/").filter(Boolean)[0] || "";
      return /^\d+$/.test(room) ? raw : "";
    } catch (_) { return ""; }
  }

  function readLaunchOptions(href) {
    const params = new URL(href || "http://127.0.0.1/").searchParams;
    return {
      roomInput: normalizeRoomInput(params.get("room_id") || params.get("room_url") || ""),
      demo: params.get("demo") === "1",
      autostart: params.get("autostart") === "1",
    };
  }

  function getTutorials() {
    return TUTORIALS.map((route) => ({
      ...route,
      steps: route.steps.map((step) => ({ ...step, expectedAction: step.expectedAction && { ...step.expectedAction } })),
      fallback: route.fallback && { steps: route.fallback.steps.map((step) => ({ ...step })) },
    }));
  }

  function bindTutorialUI(doc) {
    if (!doc || typeof doc.getElementById !== "function" || typeof doc.querySelector !== "function") return false;
    const ids = ["tutorialButton", "tutorialModal", "tutorialDialog", "tutorialClose", "guidedTour", "tourTooltip",
      "tourStepTitle", "tourStepBody", "tourStepHint", "tourProgress", "tourPrevious", "tourNext", "tourExit"];
    const ui = Object.fromEntries(ids.map((id) => [id, doc.getElementById(id)]));
    if (ids.some((id) => !ui[id])) return false;
    const modal = ui.tutorialModal;
    const dialog = ui.tutorialDialog;
    const tourLayer = ui.guidedTour;
    const tooltip = ui.tourTooltip;
    const categories = Array.from(dialog.querySelectorAll("[data-tutorial-select]"));
    const shades = Array.from(tourLayer.querySelectorAll("[data-tour-shade]"));
    if (categories.length !== TUTORIALS.length || shades.length !== 4) return false;

    let activeRoute = null;
    let activeSteps = [];
    let stepIndex = 0;
    let targetElement = null;
    let priorDescription = null;
    let actionTimer = null;
    let animationFrame = null;
    let cleanupListeners = null;
    let previousSettingsModalValue = null;
    const view = doc.defaultView || (typeof window !== "undefined" ? window : null);

    function showPicker() {
      if (activeRoute) stopTour(false);
      modal.hidden = false;
      modal.setAttribute("aria-hidden", "false");
      categories[0].focus();
    }
    function hidePicker() {
      modal.hidden = true;
      modal.setAttribute("aria-hidden", "true");
    }
    function clearDescription() {
      if (!targetElement) return;
      targetElement.classList.remove("guided-tour-target");
      if (priorDescription === null) targetElement.removeAttribute("aria-describedby");
      else targetElement.setAttribute("aria-describedby", priorDescription);
      targetElement = null;
      priorDescription = null;
    }
    function hideShades() {
      shades.forEach((shade) => {
        shade.hidden = true;
        shade.style.cssText = "";
      });
    }
    function syncSettingsSemantics() {
      const settings = doc.getElementById("settingsModal");
      if (!settings) return;
      const isOpen = settings.classList.contains("open") || settings.getAttribute("aria-hidden") === "false";
      if (isOpen && previousSettingsModalValue === null) {
        previousSettingsModalValue = settings.getAttribute("aria-modal");
        settings.setAttribute("aria-modal", "false");
      } else if (!isOpen && previousSettingsModalValue !== null) {
        settings.setAttribute("aria-modal", previousSettingsModalValue);
        previousSettingsModalValue = null;
      }
    }
    function viewport() {
      return {
        width: Math.max(1, view?.innerWidth || doc.documentElement?.clientWidth || 800),
        height: Math.max(1, view?.innerHeight || doc.documentElement?.clientHeight || 600),
      };
    }
    function visibleTarget(selector) {
      const element = doc.querySelector(selector);
      if (!element || element.disabled || element.hidden || element.closest?.("[hidden], [aria-hidden='true']")) return null;
      if (typeof element.getClientRects === "function" && !element.getClientRects().length) return null;
      return element;
    }
    function shadeTarget(rect, width, height) {
      if (!rect) return hideShades();
      const pad = 8;
      const left = Math.max(0, Math.min(width, rect.left - pad));
      const right = Math.max(0, Math.min(width, rect.right + pad));
      const top = Math.max(0, Math.min(height, rect.top - pad));
      const bottom = Math.max(0, Math.min(height, rect.bottom + pad));
      const boxes = [
        { left: 0, top: 0, width, height: top },
        { left: 0, top: bottom, width, height: Math.max(0, height - bottom) },
        { left: 0, top, width: left, height: Math.max(0, bottom - top) },
        { left: right, top, width: Math.max(0, width - right), height: Math.max(0, bottom - top) },
      ];
      boxes.forEach((box, index) => {
        const shade = shades[index];
        shade.hidden = box.width <= 0 || box.height <= 0;
        shade.style.left = `${box.left}px`;
        shade.style.top = `${box.top}px`;
        shade.style.width = `${box.width}px`;
        shade.style.height = `${box.height}px`;
      });
    }
    function placeTooltip(rect, step, missing) {
      const { width, height } = viewport();
      tooltip.classList.toggle("tour-step-unavailable", Boolean(missing));
      tooltip.style.width = "";
      tooltip.style.maxHeight = "";
      tooltip.style.maxWidth = `${Math.max(1, width - 16)}px`;
      const box = tooltip.getBoundingClientRect();
      const tipWidth = Math.min(box.width || 320, width - 16);
      const tipHeight = Math.min(box.height || 190, height - 16);
      if (!rect) {
        tooltip.style.left = `${Math.max(8, (width - tipWidth) / 2)}px`;
        tooltip.style.top = `${Math.max(8, (height - tipHeight) / 2)}px`;
        return;
      }
      const gap = 12;
      const order = [step.placement, "bottom", "top", "right", "left"].filter((value, index, all) => value !== "auto" && all.indexOf(value) === index);
      const candidates = order.map((side, preference) => {
        if (side === "top" || side === "bottom") {
          const available = Math.max(0, (side === "top" ? rect.top : height - rect.bottom) - gap - 8);
          const candidateHeight = Math.min(tipHeight, available);
          return { side, preference, width: tipWidth, height: candidateHeight,
            left: rect.left + (rect.width - tipWidth) / 2,
            top: side === "top" ? rect.top - gap - candidateHeight : rect.bottom + gap,
            area: tipWidth * candidateHeight, fits: candidateHeight >= tipHeight };
        }
        if (side === "left" || side === "right") {
          const available = Math.max(0, (side === "left" ? rect.left : width - rect.right) - gap - 8);
          const candidateWidth = Math.min(tipWidth, available);
          return { side, preference, width: candidateWidth, height: tipHeight,
            left: side === "left" ? rect.left - gap - candidateWidth : rect.right + gap,
            top: rect.top + (rect.height - tipHeight) / 2,
            area: candidateWidth * tipHeight, fits: candidateWidth >= tipWidth };
        }
        return null;
      }).filter(Boolean);
      const chosen = candidates.find((candidate) => candidate.fits) || candidates.sort((a, b) => b.area - a.area || a.preference - b.preference)[0];
      const minimumUsefulHeight = Math.min(tipHeight, Math.max(160, height * 0.55));
      if (!chosen || chosen.area === 0 || chosen.height < minimumUsefulHeight) {
        tooltip.style.maxHeight = `${Math.max(1, height - 16)}px`;
        tooltip.style.left = `${Math.max(8, (width - tipWidth) / 2)}px`;
        tooltip.style.top = "8px";
        return;
      }
      if (chosen.width < tipWidth) tooltip.style.width = `${Math.max(1, chosen.width)}px`;
      if (chosen.height < tipHeight) tooltip.style.maxHeight = `${Math.max(1, chosen.height)}px`;
      const effectiveWidth = Math.min(chosen.width || tipWidth, width - 16);
      const effectiveHeight = Math.min(chosen.height || tipHeight, height - 16);
      tooltip.style.left = `${Math.max(8, Math.min(width - effectiveWidth - 8, chosen.left))}px`;
      tooltip.style.top = `${Math.max(8, Math.min(height - effectiveHeight - 8, chosen.top))}px`;
    }
    function updatePosition() {
      if (tourLayer.hidden || !activeRoute) return;
      const step = activeSteps[stepIndex];
      const target = visibleTarget(step.target);
      if (!target) {
        clearDescription();
        hideShades();
        ui.tourStepBody.textContent = "当前这一步暂时不可用。你可以继续下一步，或退出教程。";
        placeTooltip(null, step, true);
        syncSettingsSemantics();
        return;
      }
      ui.tourStepBody.textContent = step.body;
      if (targetElement !== target) {
        clearDescription();
        targetElement = target;
        priorDescription = target.getAttribute("aria-describedby");
        target.setAttribute("aria-describedby", "tourStepBody");
        target.classList.add("guided-tour-target");
      }
      const rect = target.getBoundingClientRect();
      const { width, height } = viewport();
      if (rect.top < 8 || rect.bottom > height - 8 || rect.left < 8 || rect.right > width - 8) {
        target.scrollIntoView?.({ block: "center", inline: "nearest", behavior: "smooth" });
      }
      const freshRect = target.getBoundingClientRect();
      shadeTarget(freshRect, width, height);
      placeTooltip(freshRect, step, false);
      syncSettingsSemantics();
      if (step.type === "action" && typeof target.focus === "function") target.focus({ preventScroll: true });
      if (doc.activeElement !== target && typeof tooltip.focus === "function") tooltip.focus({ preventScroll: true });
    }
    function schedulePosition() {
      if (animationFrame !== null) return;
      const request = view?.requestAnimationFrame || ((callback) => setTimeout(callback, 0));
      animationFrame = request(() => { animationFrame = null; updatePosition(); });
    }
    function stopTour(restore = true) {
      if (actionTimer !== null) clearTimeout(actionTimer);
      actionTimer = null;
      if (animationFrame !== null) (view?.cancelAnimationFrame || clearTimeout)(animationFrame);
      animationFrame = null;
      cleanupListeners?.();
      cleanupListeners = null;
      clearDescription();
      hideShades();
      tourLayer.hidden = true;
      tourLayer.setAttribute("aria-hidden", "true");
      syncSettingsSemantics();
      const settings = doc.getElementById("settingsModal");
      if (settings && previousSettingsModalValue !== null) {
        settings.setAttribute("aria-modal", previousSettingsModalValue);
        previousSettingsModalValue = null;
      }
      activeRoute = null;
      activeSteps = [];
      stepIndex = 0;
      if (restore) {
        const settingsOpen = settings && (settings.classList.contains("open") || settings.getAttribute("aria-hidden") === "false");
        const focusTarget = settingsOpen ? doc.getElementById("roomInput") : ui.tutorialButton;
        focusTarget?.focus?.();
      }
    }
    function nextStep() {
      if (!activeRoute) return;
      if (stepIndex >= activeSteps.length - 1) return stopTour(true);
      stepIndex += 1;
      renderStep();
    }
    function bindTourListeners() {
      const actionHandler = (event) => {
        const expected = activeRoute && activeSteps[stepIndex]?.expectedAction;
        if (!expected || event.type !== expected.event || !event.target?.closest?.(expected.selector)) return;
        if (expected.validate === "room" && !normalizeRoomInput(event.target.value)) return;
        if (actionTimer !== null) return;
        const currentIndex = stepIndex;
        actionTimer = setTimeout(() => {
          actionTimer = null;
          if (activeRoute && stepIndex === currentIndex) nextStep();
        }, 0);
      };
      const keyHandler = (event) => {
        if (!activeRoute || event.key !== "Escape") return;
        event.preventDefault();
        event.stopImmediatePropagation?.();
        stopTour(true);
      };
      const resizeHandler = () => schedulePosition();
      const scrollHandler = () => schedulePosition();
      doc.addEventListener("click", actionHandler, true);
      doc.addEventListener("change", actionHandler, true);
      doc.addEventListener("keydown", keyHandler, true);
      doc.addEventListener("scroll", scrollHandler, true);
      view?.addEventListener?.("resize", resizeHandler);
      cleanupListeners = () => {
        doc.removeEventListener("click", actionHandler, true);
        doc.removeEventListener("change", actionHandler, true);
        doc.removeEventListener("keydown", keyHandler, true);
        doc.removeEventListener("scroll", scrollHandler, true);
        view?.removeEventListener?.("resize", resizeHandler);
      };
    }
    function renderStep() {
      const step = activeSteps[stepIndex];
      if (!step) return stopTour(true);
      ui.tourProgress.textContent = `第 ${stepIndex + 1} 步，共 ${activeSteps.length} 步`;
      ui.tourStepTitle.textContent = step.title;
      ui.tourStepBody.textContent = step.body;
      ui.tourStepHint.textContent = step.hint || (step.type === "action" ? "完成这个操作会自动继续；也可以点“跳过 / 下一步”。" : "");
      ui.tourStepHint.hidden = !ui.tourStepHint.textContent;
      ui.tourPrevious.hidden = stepIndex === 0;
      ui.tourNext.textContent = stepIndex === activeSteps.length - 1 ? "完成" : step.type === "action" ? "跳过 / 下一步" : "下一步";
      if (!visibleTarget(step.target)) ui.tourStepBody.textContent = "当前这一步暂时不可用。你可以继续下一步，或退出教程。";
      tourLayer.hidden = false;
      tourLayer.setAttribute("aria-hidden", "false");
      syncSettingsSemantics();
      updatePosition();
    }
    function beginRoute(routeId) {
      const route = TUTORIALS.find((item) => item.id === routeId);
      if (!route) return;
      activeRoute = route;
      activeSteps = route.steps;
      stepIndex = 0;
      if (!visibleTarget(activeSteps[0].target) && route.fallback) activeSteps = route.fallback.steps;
      hidePicker();
      bindTourListeners();
      renderStep();
    }

    ui.tutorialButton.addEventListener("click", showPicker);
    ui.tutorialClose.addEventListener("click", () => { hidePicker(); ui.tutorialButton.focus(); });
    categories.forEach((button) => button.addEventListener("click", () => beginRoute(button.dataset.tutorialSelect)));
    modal.addEventListener("click", (event) => {
      if (event.target === modal) { hidePicker(); ui.tutorialButton.focus(); }
    });
    dialog.addEventListener("keydown", (event) => {
      if (modal.hidden) return;
      if (event.key === "Escape") {
        event.preventDefault();
        hidePicker();
        ui.tutorialButton.focus();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(dialog.querySelectorAll("button:not([disabled]), a[href], input, select, textarea, [tabindex]:not([tabindex='-1'])"))
        .filter((element) => !element.disabled && !element.closest("[hidden]"));
      if (focusable.length && event.shiftKey && (doc.activeElement === focusable[0] || !focusable.includes(doc.activeElement))) {
        event.preventDefault();
        focusable[focusable.length - 1].focus();
      } else if (focusable.length && !event.shiftKey && (doc.activeElement === focusable[focusable.length - 1] || !focusable.includes(doc.activeElement))) {
        event.preventDefault();
        focusable[0].focus();
      }
    });
    ui.tourPrevious.addEventListener("click", () => {
      if (stepIndex > 0) { stepIndex -= 1; renderStep(); }
    });
    ui.tourNext.addEventListener("click", nextStep);
    ui.tourExit.addEventListener("click", () => stopTour(true));
    return true;
  }

  return { normalizeRoomInput, readLaunchOptions, getTutorials, bindTutorialUI };
});
