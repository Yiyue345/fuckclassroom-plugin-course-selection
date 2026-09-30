(() => {
  const form = document.querySelector("[data-catalog-form]");
  if (!form) return;

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

  const body = document.querySelector("[data-catalog-body]");
  const status = document.querySelector("[data-catalog-status]");
  const statusText = document.querySelector("[data-catalog-status-text]");
  const total = document.querySelector("[data-catalog-total]");
  const updated = document.querySelector("[data-catalog-updated]");
  const pageText = document.querySelector("[data-catalog-page]");
  const pageSize = document.querySelector("#catalog-page-size");
  const previous = document.querySelector("[data-catalog-prev]");
  const next = document.querySelector("[data-catalog-next]");
  const refresh = document.querySelector("[data-catalog-refresh]");
  const queueBatch = document.querySelector("#catalog-queue-batch");
  const teacherTerm = document.querySelector("#catalog-teacher-term");
  const teacherSelect = document.querySelector("#catalog-teacher");
  let currentPage = 1;
  let totalPages = 1;
  let loading = false;
  let teacherTimer = 0;

  const optionTargets = {
    semesters: ["#catalog-semester", "当前学期"],
    departments: ["#catalog-department", "全部院系"],
    campuses: ["#catalog-campus", "全部校区"],
    course_types: ["#catalog-course-type", "全部类别"],
    exam_modes: ["#catalog-exam-mode", "全部方式"],
    grades: ["#catalog-grade", "全部年级"],
    weekdays: ["#catalog-weekday", "全部星期"],
    periods: ["#catalog-periods", ""],
  };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function showStatus(message, kind = "") {
    if (disposed()) return;
    status.classList.remove("hidden", "error", "success");
    if (kind) status.classList.add(kind);
    statusText.textContent = message;
  }

  function hideStatus() {
    if (disposed()) return;
    status.classList.add("hidden");
  }

  async function responseError(response, fallback) {
    try {
      const payload = await response.json();
      return payload.detail || payload.error || fallback;
    } catch {
      return fallback;
    }
  }

  function populateOptions(options, semesterId) {
    Object.entries(optionTargets).forEach(([key, target]) => {
      const select = document.querySelector(target[0]);
      const rows = Array.isArray(options[key]) ? options[key] : [];
      const selected = key === "semesters" ? (select.value || semesterId || "") : select.value;
      const nodes = [];
      if (target[1]) nodes.push(new Option(target[1], ""));
      rows.forEach((item) => nodes.push(new Option(item.label || item.value, item.value)));
      select.replaceChildren(...nodes);
      if (selected && Array.from(select.options).some((item) => item.value === String(selected))) {
        select.value = String(selected);
      }
    });
  }

  function populateQueueBatches(batches) {
    const selected = queueBatch.value;
    const nodes = [new Option("请选择队列批次", ""), new Option("等待新批次自动匹配", "pending")];
    (Array.isArray(batches) ? batches : []).forEach((batch) => {
      const key = String(batch.student_id) + ":" + String(batch.turn_id);
      nodes.push(new Option(batch.label || ("选课批次 " + batch.turn_id), key));
    });
    queueBatch.replaceChildren(...nodes);
    if (selected && Array.from(queueBatch.options).some((item) => item.value === selected)) {
      queueBatch.value = selected;
    }
  }

  function capacityNode(course) {
    const wrap = element("div", "capacity");
    if (course.selected_count === null || course.max_count === null || !course.max_count) {
      wrap.append(element("span", "muted", "未公布"));
      return wrap;
    }
    const bar = element("div", "progress");
    const fill = element("span");
    const percent = Math.min(100, Math.max(0, course.selected_count * 100 / course.max_count));
    fill.style.width = percent + "%";
    if (course.is_full) fill.style.background = "var(--red)";
    else if (percent > 85) fill.style.background = "var(--amber)";
    else fill.style.background = "var(--green)";
    bar.append(fill);
    wrap.append(bar, element("span", "capacity-label", course.selected_count + "/" + course.max_count));
    return wrap;
  }

  function actionButton(label, className, action, lessonId) {
    const button = element("button", "btn small " + className, label);
    button.type = "button";
    button.addEventListener("click", () => runAction(button, action, lessonId));
    return button;
  }

  function queueButton(course) {
    const button = element(
      "button",
      "btn small",
      course.is_queued ? "已在队列" : "加入队列"
    );
    button.type = "button";
    button.disabled = Boolean(course.is_queued);
    button.addEventListener("click", async () => {
      if (!queueBatch.value) {
        showStatus("请先选择定时选课批次", "error");
        queueBatch.focus();
        return;
      }
      button.disabled = true;
      showStatus("正在加入定时队列");
      try {
        const response = await pageFetch("/selection/catalog/queue", {
          method: "POST",
          body: new URLSearchParams({
            lesson_id: String(course.lesson_id),
            batch_key: queueBatch.value,
          }),
          headers: {Accept: "application/json"},
        });
        if (!response.ok) throw new Error(await responseError(response, "加入队列失败"));
        const payload = await response.json();
        if (disposed()) return;
        button.textContent = "已在队列";
        showStatus(payload.message || "已加入定时队列", "success");
      } catch (error) {
        if (disposed() || error?.name === "AbortError") return;
        button.disabled = false;
        showStatus(error.message || "加入队列失败", "error");
      }
    });
    return button;
  }

  function renderCourse(course) {
    const row = document.createElement("tr");

    const courseCell = document.createElement("td");
    courseCell.append(
      element("div", "table-title", course.course_name || "未命名课程"),
      element(
        "div",
        "table-subtitle",
        [course.course_code, course.credits !== null && course.credits !== undefined ? course.credits + " 学分" : ""]
          .filter(Boolean).join(" · ") || "课程信息未公布"
      )
    );
    if (Array.isArray(course.matched_targets) && course.matched_targets.length) {
      courseCell.append(element("span", "status blue catalog-match", "目标匹配"));
    }

    const lessonCell = document.createElement("td");
    const lessonName = element("div", "table-title compact selection-lesson-name", course.lesson_name || "教学班名称未公布");
    lessonName.title = lessonName.textContent;
    lessonCell.append(
      lessonName,
      element("div", "table-subtitle", [course.teachers || "教师未公布", course.lesson_code].filter(Boolean).join(" · "))
    );

    const departmentCell = document.createElement("td");
    departmentCell.append(
      element("div", "catalog-cell-main", course.department || "院系未公布"),
      element("div", "table-subtitle", [course.course_type, course.course_property].filter(Boolean).join(" · "))
    );

    const scheduleCell = document.createElement("td");
    const schedule = element("div");
    window.FuckClassroomSelectionDisplay.renderSchedule(schedule, course.time);
    scheduleCell.append(
      schedule,
      element("div", "table-subtitle", course.campus || "")
    );

    const capacityCell = document.createElement("td");
    capacityCell.append(capacityNode(course));

    const stateCell = document.createElement("td");
    const state = element(
      "span",
      "status " + (course.is_selected ? "green" : course.is_full ? "red" : "blue"),
      course.is_selected ? "已选" : course.is_full ? "已满" : "可尝试"
    );
    stateCell.append(state);

    const actionsCell = element("td", "actions-cell");
    const actions = element("div", "catalog-row-actions");
    if (course.is_selected) {
      actions.append(actionButton("退课", "danger", "drop", course.lesson_id));
    } else {
      actions.append(actionButton("选课", "primary", "select", course.lesson_id), queueButton(course));
    }
    actionsCell.append(actions);

    row.append(courseCell, lessonCell, departmentCell, scheduleCell, capacityCell, stateCell, actionsCell);
    return row;
  }

  function render(payload) {
    if (disposed()) return;
    const courses = Array.isArray(payload.courses) ? payload.courses : [];
    const pagination = payload.pagination || {};
    currentPage = Number(pagination.page) || 1;
    totalPages = Math.max(1, Number(pagination.pages) || 1);
    body.replaceChildren(...(courses.length
      ? courses.map(renderCourse)
      : [(() => {
          const row = document.createElement("tr");
          const cell = element("td", "table-empty", "没有符合条件的教学班");
          cell.colSpan = 7;
          row.append(cell);
          return row;
        })()]));
    total.textContent = (Number(pagination.total) || 0) + " 个教学班";
    updated.textContent = payload.updated_at ? "更新于 " + new Date(payload.updated_at).toLocaleString("zh-CN") : "";
    pageText.textContent = "第 " + currentPage + " / " + totalPages + " 页";
    previous.disabled = currentPage <= 1 || payload.targets_only;
    next.disabled = currentPage >= totalPages || payload.targets_only;
    populateOptions(payload.options || {}, payload.semester_id);
    populateQueueBatches(payload.selection_batches || []);
  }

  async function load(page = 1) {
    if (loading) return;
    loading = true;
    form.setAttribute("aria-busy", "true");
    showStatus("正在查询全校开课");
    body.replaceChildren((() => {
      const row = document.createElement("tr");
      const cell = element("td", "table-empty", "正在读取课程");
      cell.colSpan = 7;
      row.append(cell);
      return row;
    })());
    try {
      const params = new URLSearchParams(new FormData(form));
      params.set("page", String(page));
      params.set("page_size", pageSize.value);
      const response = await pageFetch("/api/selection/catalog?" + params.toString(), {
        headers: {Accept: "application/json"},
        cache: "no-store",
      });
      if (!response.ok) throw new Error(await responseError(response, "查询全校开课失败"));
      const payload = await response.json();
      if (disposed()) return;
      render(payload);
      const visibleParams = new URLSearchParams(params);
      visibleParams.delete("page");
      if (!disposed()) {
        const visibleUrl = "/selection/catalog?" + visibleParams.toString();
        if (window.FuckClassroomShell?.replaceUrl) {
          window.FuckClassroomShell.replaceUrl(visibleUrl, { scrollY: window.scrollY });
        } else {
          history.replaceState(
            { ...(history.state || {}), scrollY: window.scrollY },
            "",
            visibleUrl
          );
        }
      }
      hideStatus();
    } catch (error) {
      if (disposed() || error?.name === "AbortError") return;
      showStatus(error.message || "查询全校开课失败", "error");
      const row = document.createElement("tr");
      const cell = element("td", "table-empty", "课程读取失败");
      cell.colSpan = 7;
      row.append(cell);
      body.replaceChildren(row);
    } finally {
      if (disposed()) return;
      loading = false;
      form.removeAttribute("aria-busy");
    }
  }

  async function runAction(button, action, lessonId) {
    if (action === "drop" && !window.confirm("确认退选该教学班？")) return;
    button.disabled = true;
    const label = action === "drop" ? "退课" : "选课";
    showStatus("正在启动" + label + "任务");
    try {
      const response = await pageFetch("/selection/catalog/action", {
        method: "POST",
        body: new URLSearchParams({lesson_id: String(lessonId), action}),
        headers: {Accept: "application/json"},
      });
      if (!response.ok) throw new Error(await responseError(response, "启动" + label + "失败"));
      const started = await response.json();
      while (!disposed()) {
        const taskResponse = await pageFetch(started.status_url, {
          headers: {Accept: "application/json"},
          cache: "no-store",
        });
        if (!taskResponse.ok) throw new Error("读取" + label + "进度失败");
        const task = await taskResponse.json();
        if (disposed()) return;
        showStatus(task.error || task.message || ("正在" + label));
        if (task.status === "failed") throw new Error(task.error || label + "失败");
        if (task.status === "succeeded") break;
        await pageSleep(300);
      }
      if (disposed()) return;
      let message = label + "成功";
      try {
        const resultResponse = await pageFetch("/api/selection/action-result", {cache: "no-store"});
        if (resultResponse.ok) {
          const result = await resultResponse.json();
          message = result.message || message;
        }
      } catch {
        message = label + "成功";
      }
      if (disposed()) return;
      showStatus(message, "success");
      await load(currentPage);
    } catch (error) {
      if (disposed() || error?.name === "AbortError") return;
      button.disabled = false;
      showStatus(error.message || label + "失败", "error");
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    load(1);
  });
  form.addEventListener("reset", () => {
    pageTimeout(() => {
      teacherSelect.classList.add("hidden");
      teacherSelect.replaceChildren();
      load(1);
    }, 0);
  });
  refresh.addEventListener("click", () => load(currentPage));
  pageSize.addEventListener("change", () => load(1));
  previous.addEventListener("click", () => load(Math.max(1, currentPage - 1)));
  next.addEventListener("click", () => load(Math.min(totalPages, currentPage + 1)));

  teacherTerm.addEventListener("input", () => {
    window.clearTimeout(teacherTimer);
    teacherSelect.value = "";
    const term = teacherTerm.value.trim();
    if (term.length < 2) {
      teacherSelect.classList.add("hidden");
      return;
    }
    teacherTimer = pageTimeout(async () => {
      try {
        const response = await pageFetch("/api/selection/catalog/teachers?q=" + encodeURIComponent(term), {
          headers: {Accept: "application/json"},
          cache: "no-store",
        });
        if (!response.ok) return;
        const payload = await response.json();
        if (disposed()) return;
        const teachers = Array.isArray(payload.teachers) ? payload.teachers : [];
        const options = [new Option("请选择教师", "")];
        teachers.forEach((teacher) => {
          const detail = [teacher.name, teacher.code, teacher.department].filter(Boolean).join(" · ");
          options.push(new Option(detail, teacher.id));
        });
        teacherSelect.replaceChildren(...options);
        teacherSelect.classList.toggle("hidden", teachers.length === 0);
      } catch (error) {
        if (disposed() || error?.name === "AbortError") return;
        teacherSelect.classList.add("hidden");
      }
    }, 250);
  });

  const initial = new URLSearchParams(window.location.search);
  initial.forEach((value, key) => {
    const field = form.elements.namedItem(key);
    if (!field) return;
    if (field instanceof RadioNodeList) return;
    if (field.type === "checkbox") field.checked = value === "1";
    else if (!["semester_id", "department_id", "campus_id", "course_type_id", "exam_mode_id", "grade", "weekday", "periods"].includes(key)) field.value = value;
  });
  if (initial.get("page_size")) pageSize.value = initial.get("page_size");
  load(1);
})();
