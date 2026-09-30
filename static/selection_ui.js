(() => {
  const ui = window.FuckClassroomUI;
  if (!ui) return;
  const { readResponseError, showToast } = ui;
  const pageRuntime = window.FuckClassroomPage?.current?.();
  const pageSignal = pageRuntime?.signal;
  const pageTimeout = pageRuntime?.setTimeout
    ? (callback, delay) => pageRuntime.setTimeout(callback, delay)
    : (callback, delay) => window.setTimeout(callback, delay);
  const pageSleep = pageRuntime?.sleep
    ? (delay) => pageRuntime.sleep(delay)
    : (delay) => new Promise((resolve) => window.setTimeout(resolve, delay));

  function disposed() {
    return Boolean(pageSignal?.aborted);
  }

  function pageFetch(input, init = {}) {
    return window.fetch(input, { ...init, signal: pageSignal });
  }
  function applySelectionActionState(form, action) {
    const row = form.closest("tr[data-lesson-id]");
    const button = form.querySelector("[data-selection-submit]");
    if (!row || !button) return;
    const selected = action === "select";
    row.dataset.selected = selected ? "true" : "false";
    const statusCell = row.querySelector("[data-status-cell]");
    if (statusCell) {
      const status = document.createElement("span");
      status.className = "status " + (selected ? "green" : "blue");
      status.textContent = selected ? "已选" : "可选";
      statusCell.replaceChildren(status);
    }
    form.dataset.selectionAction = selected ? "drop" : "select";
    form.action = selected ? "/selection/info/drop" : "/selection/info/select";
    if (selected) {
      form.dataset.confirm = "确认退选 " + (form.dataset.courseName || "这门课程") + "？";
    } else {
      delete form.dataset.confirm;
    }
    button.classList.toggle("primary", !selected);
    button.classList.toggle("danger", selected);
    button.textContent = selected ? "退课" : "选课";
    button.disabled = false;
  }

  document.querySelectorAll("form[data-selection-action]").forEach((form) => {
    const statusBox = document.querySelector(form.dataset.taskStatus || "");
    const submitButton = form.querySelector("[data-selection-submit]");
    const message = statusBox && statusBox.querySelector("[data-task-message]");
    const progress = statusBox && statusBox.querySelector("[data-task-progress]");
    if (!statusBox || !submitButton || !message || !progress) return;

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (statusBox.dataset.taskRunning === "true") return;
      const action = form.dataset.selectionAction || "select";
      if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
      const actionLabel = action === "drop" ? "退课" : "选课";

      statusBox.dataset.taskRunning = "true";
      statusBox.classList.remove("hidden", "error", "success");
      message.textContent = "正在启动" + actionLabel + "任务";
      progress.style.width = "4%";
      submitButton.disabled = true;

      try {
        const response = await pageFetch(form.action, {
          method: "POST",
          body: new URLSearchParams(new FormData(form)),
          headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error(await readResponseError(response, "启动" + actionLabel + "失败"));
        const started = await response.json();
        if (!started.status_url) throw new Error(actionLabel + "任务没有返回状态地址");

        while (!disposed()) {
          const taskResponse = await pageFetch(started.status_url, {
            headers: { Accept: "application/json" },
            cache: "no-store",
          });
          if (!taskResponse.ok) throw new Error("读取" + actionLabel + "进度失败（" + taskResponse.status + "）");
          const task = await taskResponse.json();
          if (disposed()) return;
          const percent = Math.max(0, Math.min(100, Number(task.progress) || 0));
          progress.style.width = percent + "%";
          message.textContent = task.error || task.message || ("正在" + actionLabel);
          if (task.status === "failed") throw new Error(task.error || (actionLabel + "失败"));
          if (task.status === "succeeded") break;
          await pageSleep(250);
        }

        let result = {};
        try {
          const resultResponse = await pageFetch("/api/selection/action-result", { cache: "no-store" });
          if (resultResponse.ok) result = await resultResponse.json();
        } catch {
          result = {};
        }
        if (disposed()) return;
        const resultMessage = result.message || (actionLabel + "成功");
        statusBox.classList.add("success");
        message.textContent = resultMessage;
        progress.style.width = "100%";
        applySelectionActionState(form, action);
        showToast(actionLabel + "成功", (form.dataset.courseName || "课程") + "：" + resultMessage);
        pageTimeout(() => statusBox.classList.add("hidden"), 4000);
      } catch (error) {
        if (disposed() || error?.name === "AbortError") return;
        const errorMessage = error && error.message ? error.message : (actionLabel + "失败");
        statusBox.classList.add("error");
        message.textContent = errorMessage;
        progress.style.width = "100%";
        submitButton.disabled = false;
        showToast(actionLabel + "未完成", errorMessage, true);
      } finally {
        if (!disposed()) statusBox.dataset.taskRunning = "false";
      }
    });
  });

  document.querySelectorAll("form[data-auto-selection]").forEach((form) => {
    const button = form.querySelector("[data-auto-selection-submit]");
    const label = button && button.querySelector("span");
    if (!button || !label) return;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (button.disabled) return;
      button.disabled = true;
      const original = label.textContent;
      label.textContent = "加入中";
      try {
        const response = await pageFetch(form.action, {
          method: "POST",
          body: new URLSearchParams(new FormData(form)),
          headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error(await readResponseError(response, "加入定时选课失败"));
        const payload = await response.json();
        if (disposed()) return;
        label.textContent = "已定时";
        showToast("定时选课已保存", payload.message || (form.dataset.courseName + " 已加入队列"));
      } catch (error) {
        if (disposed() || error?.name === "AbortError") return;
        button.disabled = false;
        label.textContent = original;
        showToast("未能加入定时选课", error && error.message ? error.message : "请求失败", true);
      }
    });
  });

  const autoJobList = document.querySelector("[data-auto-job-list]");
  if (autoJobList) {
    const statusColors = {
      selected: "green",
      full: "red",
      failed: "red",
      expired: "red",
      cancelled: "gray",
      scheduled: "blue",
      attempting: "blue",
      retrying: "blue",
      waiting_session: "blue",
      waiting_capacity: "blue",
      checking_capacity: "blue",
    };
    const refreshAutoJobs = async () => {
      if (!document.hidden) {
        try {
          const response = await pageFetch("/api/selection/auto/jobs", { cache: "no-store" });
          if (response.ok) {
            const payload = await response.json();
            if (disposed()) return;
            (payload.jobs || []).forEach((job) => {
              const row = autoJobList.querySelector('[data-auto-job-id="' + CSS.escape(job.id) + '"]');
              if (!row) return;
              const status = row.querySelector("[data-auto-job-status]");
              const message = row.querySelector("[data-auto-job-message]");
              const next = row.querySelector("[data-auto-job-next]");
              const attempts = row.querySelector("[data-auto-job-attempts]");
              const checks = row.querySelector("[data-auto-job-checks]");
              const capacity = row.querySelector("[data-auto-job-capacity]");
              const last = row.querySelector("[data-auto-job-last]");
              if (status) {
                status.className = "status " + (statusColors[job.status] || "gray");
                status.textContent = job.status_label;
              }
              if (message) message.textContent = job.message || "";
              if (next) next.textContent = job.next_attempt_at ? ("下次 " + job.next_attempt_at) : "";
              if (attempts) attempts.textContent = String(job.attempts || 0);
              if (checks) checks.textContent = "监测 " + String(job.capacity_checks || 0) + " 次";
              if (capacity) {
                capacity.textContent = job.max_count
                  ? "人数 " + (job.last_selected_count ?? "未知") + "/" + job.max_count
                  : "";
              }
              if (last) last.textContent = job.last_attempt_at || "尚未提交";
            });
          }
        } catch (error) {
          if (disposed() || error?.name === "AbortError") return;
          // Keep the last known state; the next poll will retry.
        }
      }
      if (!disposed()) pageTimeout(refreshAutoJobs, 3000);
    };
    pageTimeout(refreshAutoJobs, 3000);
  }


  const countRefreshToggle = document.querySelector("[data-count-refresh-toggle]");
  const countRefreshStatus = document.querySelector("[data-count-refresh-status]");
  if (countRefreshToggle && countRefreshStatus) {
    const storageKey = "selection-count-refresh-enabled";
    let refreshTimer = 0;
    let refreshRunning = false;

    const scheduleRefresh = () => {
      window.clearTimeout(refreshTimer);
      if (countRefreshToggle.checked && !document.hidden) {
        refreshTimer = pageTimeout(refreshCounts, 30000);
      }
    };

    const renderCount = (lessonId, countInfo) => {
      if (disposed()) return;
      const row = document.querySelector('tr[data-lesson-id="' + CSS.escape(String(lessonId)) + '"]');
      if (!row) return;
      const selectedCount = Number(countInfo && countInfo.selected_count);
      const apiMax = Number(countInfo && countInfo.max_count);
      const cachedMax = Number(row.dataset.maxCount);
      const maxCount = Number.isFinite(apiMax) && apiMax > 0 ? apiMax : cachedMax;
      if (!Number.isFinite(selectedCount) || !Number.isFinite(maxCount) || maxCount <= 0) return;

      row.dataset.maxCount = String(maxCount);
      const full = selectedCount >= maxCount;
      const percent = Math.max(0, Math.min(100, selectedCount * 100 / maxCount));
      const capacityCell = row.querySelector("[data-capacity-cell]");
      if (capacityCell) {
        const capacity = document.createElement("div");
        capacity.className = "capacity";
        const progressTrack = document.createElement("div");
        progressTrack.className = "progress";
        const progressBar = document.createElement("span");
        progressBar.style.width = percent + "%";
        progressBar.style.background = full ? "var(--red)" : (percent > 85 ? "var(--amber)" : "var(--green)");
        progressTrack.appendChild(progressBar);
        const label = document.createElement("span");
        label.className = "capacity-label";
        label.textContent = selectedCount + "/" + maxCount;
        capacity.append(progressTrack, label);
        capacityCell.replaceChildren(capacity);
      }

      if (row.dataset.selected !== "true") {
        const statusCell = row.querySelector("[data-status-cell]");
        if (statusCell) {
          const status = document.createElement("span");
          status.className = "status " + (full ? "red" : "blue");
          status.textContent = full ? "已满" : "可选";
          statusCell.replaceChildren(status);
        }
        const form = row.querySelector('form[data-selection-action="select"]');
        const button = form && form.querySelector("[data-selection-submit]");
        if (button) button.disabled = full;
      }
    };

    async function refreshCounts() {
      if (disposed()) return;
      if (refreshRunning || !countRefreshToggle.checked || document.hidden) {
        scheduleRefresh();
        return;
      }
      refreshRunning = true;
      countRefreshStatus.textContent = "刷新中";
      try {
        const response = await pageFetch("/api/selection/counts", {
          headers: { Accept: "application/json" },
          cache: "no-store",
        });
        if (!response.ok) {
          const errorMessage = await readResponseError(response, "人数刷新失败");
          if (response.status === 409) {
            countRefreshToggle.checked = false;
            localStorage.setItem(storageKey, "false");
          }
          throw new Error(errorMessage);
        }
        const payload = await response.json();
        if (disposed()) return;
        Object.entries(payload.courses || {}).forEach(([lessonId, countInfo]) => renderCount(lessonId, countInfo));
        countRefreshStatus.textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      } catch (error) {
        if (disposed() || error?.name === "AbortError") return;
        countRefreshStatus.textContent = error && error.message ? error.message : "人数刷新失败";
      } finally {
        if (disposed()) return;
        refreshRunning = false;
        scheduleRefresh();
      }
    }

    countRefreshToggle.checked = localStorage.getItem(storageKey) === "true";
    countRefreshToggle.addEventListener("change", () => {
      localStorage.setItem(storageKey, String(countRefreshToggle.checked));
      if (countRefreshToggle.checked) {
        refreshCounts();
      } else {
        window.clearTimeout(refreshTimer);
        countRefreshStatus.textContent = "已暂停";
      }
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        window.clearTimeout(refreshTimer);
      } else if (countRefreshToggle.checked) {
        refreshCounts();
      }
    }, { signal: pageSignal });
    if (countRefreshToggle.checked) refreshCounts();
  }

})();
