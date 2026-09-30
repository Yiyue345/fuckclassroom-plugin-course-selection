(() => {
  const unpublished = /^(?:(?:上课)?(?:时间地点|时间|地点|教室))?未公布$/;

  // Keep unfamiliar formats intact rather than guessing a classroom or losing text.
  function parseSchedule(value) {
    const records = String(value || "").split(/(?:\r?\n|[；;]|<br\s*\/?\s*>)+/i)
      .map((text) => text.trim()).filter(Boolean);
    return [...new Set(records)].filter((text) => {
      if (unpublished.test(text)) return false;
      // The upstream system emits one empty lunch slot per weekday.
      // Only discard bare placeholders; keep records containing a real location or note.
      return !/^(?:第?\d+(?:\s*[~～—–-]\s*\d+)?周\s*)?(?:星期|周|礼拜)[一二三四五六日天1-7]\s*中午\s*[~～—–-]\s*中午节\s*(?:(?:教室|地点)?未公布)?$/.test(text);
    }).map((text) => {
        const match = text.match(/^(.*?(?:星期|周|礼拜)[一二三四五六日天1-7].*?\d\s*节(?:\s*[（(][单双]周[）)])?)[\s,，:：]*(.*)$/);
        return match
          ? {
              time: match[1].trim().replace(/(\d+)\s*[~～—–-]\s*(\d+)(\s*节)/g,
                (range, start, end, suffix) => start === end ? start + suffix : range),
              room: unpublished.test(match[2].trim()) ? "" : match[2].trim(),
              raw: text,
            }
          : { time: "", room: "", raw: text };
      });
  }

  function renderSchedule(container, value) {
    container.className = "selection-schedule";
    const entries = parseSchedule(value);
    if (!entries.length) {
      container.textContent = "时间地点未公布";
      return;
    }
    container.replaceChildren(...entries.map((entry) => {
      const card = document.createElement("div");
      card.className = "selection-schedule-card";
      if (!entry.time) {
        card.textContent = entry.raw;
        return card;
      }
      const time = document.createElement("div");
      time.className = "selection-schedule-time";
      time.textContent = entry.time;
      card.append(time);
      if (entry.room) {
        const room = document.createElement("div");
        room.className = "selection-schedule-room";
        room.textContent = entry.room;
        card.append(room);
      }
      return card;
    }));
  }

  window.FuckClassroomSelectionDisplay = { parseSchedule, renderSchedule };
  document.querySelectorAll("[data-selection-schedule]").forEach((node) => {
    renderSchedule(node, node.textContent);
  });
})();
