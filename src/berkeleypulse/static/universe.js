(function () {
  var root = document.querySelector("[data-universe]");
  if (!root) return;
  var launch = root.querySelector(".universe-launch");
  var close = root.querySelector(".universe-close");
  var form = root.querySelector(".universe-box");
  var area = form.querySelector("textarea");
  var plus = form.querySelector(".universe-plus");
  var send = form.querySelector(".universe-send");
  var fileInput = form.querySelector(".universe-file");
  var strip = form.querySelector(".universe-images");
  var note = form.querySelector(".universe-note");
  var hint = form.querySelector(".universe-hint");
  var count = form.querySelector(".universe-count");
  var thinking = form.querySelector(".universe-thinking");
  var proposal = root.querySelector("[data-proposal]");
  var proposalCount = proposal.querySelector(".proposal-count");
  var proposalOld = proposal.querySelector("[data-proposal-old]");
  var proposalNew = proposal.querySelector("[data-proposal-new]");
  var modes = form.querySelectorAll('input[name="mode"]');
  var files = [];
  var ticket = 0;
  var inflight = null;
  var previews = [];
  var limit = 10;
  var maxBytes = 8000000;
  var copy = {
    plan: {
      placeholder: "Scribble a class, deadline, or block",
      hint: "A proposed calendar addition"
    },
    study: {
      placeholder: "Ask for resources, a study plan, or blocked time",
      hint: "Course guides and a study block"
    }
  };

  function chosenMode() {
    var selected = form.querySelector('input[name="mode"]:checked');
    return selected && copy[selected.value] ? selected.value : "plan";
  }

  function applyMode() {
    var text = copy[chosenMode()];
    area.placeholder = text.placeholder;
    hint.textContent = text.hint;
  }

  function setOpen(open) {
    root.classList.toggle("is-open", open);
    launch.setAttribute("aria-expanded", open ? "true" : "false");
    form.hidden = !open;
    if (!open) {
      ticket += 1;
      if (inflight) inflight.abort();
      setThinking(false);
      proposal.hidden = true;
    }
    if (open) window.requestAnimationFrame(function () { area.focus(); });
  }

  function clearBox() {
    ticket += 1;
    if (inflight) inflight.abort();
    area.value = "";
    files = [];
    setThinking(false);
    proposal.hidden = true;
    proposalOld.replaceChildren();
    proposalNew.replaceChildren();
    proposalCount.textContent = "";
    showNote("");
    render();
    grow();
  }

  function setThinking(on) {
    form.classList.toggle("is-thinking", on);
    form.setAttribute("aria-busy", on ? "true" : "false");
    thinking.hidden = !on;
    if (on) showNote("");
    syncSend();
  }

  function fillColumn(target, days, side) {
    target.replaceChildren();
    days.forEach(function (day) {
      var block = document.createElement("div");
      var heading = document.createElement("h3");
      block.className = "proposal-day";
      heading.textContent = day.label;
      block.appendChild(heading);
      (day.rows || []).forEach(function (row) {
        var line = document.createElement("div");
        var clock = document.createElement("time");
        var items = document.createElement("div");
        var list = row[side] || [];
        line.className = "proposal-row";
        clock.textContent = row.time;
        items.className = "proposal-items";
        if (!list.length) {
          var empty = document.createElement("span");
          empty.className = "proposal-empty";
          empty.textContent = "—";
          items.appendChild(empty);
        }
        list.forEach(function (item) {
          var chip = document.createElement("article");
          var strong = document.createElement("strong");
          chip.className = "proposal-item" + (item.tone ? " " + item.tone : "");
          strong.textContent = item.title;
          chip.appendChild(strong);
          if (item.span) {
            var span = document.createElement("span");
            span.textContent = item.span;
            chip.appendChild(span);
          }
          items.appendChild(chip);
        });
        line.appendChild(clock);
        line.appendChild(items);
        block.appendChild(line);
      });
      target.appendChild(block);
    });
  }

  function showProposal(data) {
    proposalCount.textContent = data.summary || "";
    fillColumn(proposalOld, data.days || [], "old");
    fillColumn(proposalNew, data.days || [], "new");
    proposal.hidden = false;
  }

  function showNote(text) {
    note.hidden = !text;
    note.textContent = text || "";
  }

  function syncSend() {
    send.disabled = form.classList.contains("is-thinking") || (!area.value.trim() && files.length === 0);
  }

  function grow() {
    area.style.height = "auto";
    area.style.height = Math.min(area.scrollHeight, 168) + "px";
  }

  function render() {
    previews.forEach(function (url) { URL.revokeObjectURL(url); });
    previews = [];
    strip.replaceChildren();
    files.forEach(function (file, index) {
      var url = URL.createObjectURL(file);
      var item = document.createElement("li");
      var img = document.createElement("img");
      var remove = document.createElement("button");
      previews.push(url);
      img.src = url;
      img.alt = "";
      remove.type = "button";
      remove.setAttribute("aria-label", "Remove image");
      remove.textContent = "×";
      remove.addEventListener("click", function (event) {
        event.stopPropagation();
        files.splice(index, 1);
        showNote("");
        render();
      });
      item.appendChild(img);
      item.appendChild(remove);
      strip.appendChild(item);
    });
    strip.hidden = files.length === 0;
    count.hidden = files.length === 0;
    count.textContent = files.length + "/10";
    plus.setAttribute("aria-label", files.length ? "Add images, " + files.length + " of 10" : "Add images");
    syncSend();
  }

  function addFiles(list) {
    var message = "";
    Array.prototype.forEach.call(list || [], function (file) {
      if (!/^image\/(png|jpeg|gif|webp)$/.test(file.type)) {
        message = "Use a PNG, JPEG, GIF, or WebP image.";
        return;
      }
      if (file.size > maxBytes) {
        message = "Each image needs to be 8 MB or smaller.";
        return;
      }
      if (files.length >= limit) {
        message = "10 images is the limit.";
        return;
      }
      files.push(file);
    });
    showNote(message);
    render();
  }

  function hasFiles(event) {
    var types = event.dataTransfer && event.dataTransfer.types;
    return types && Array.prototype.indexOf.call(types, "Files") !== -1;
  }

  function watchDrop(target) {
    target.addEventListener("dragenter", function (event) {
      if (!hasFiles(event)) return;
      event.preventDefault();
      root.classList.add("is-dropping");
    });
    target.addEventListener("dragover", function (event) {
      if (!hasFiles(event)) return;
      event.preventDefault();
    });
    target.addEventListener("dragleave", function (event) {
      if (target.contains(event.relatedTarget)) return;
      root.classList.remove("is-dropping");
    });
    target.addEventListener("drop", function (event) {
      if (!hasFiles(event)) return;
      event.preventDefault();
      root.classList.remove("is-dropping");
      setOpen(true);
      addFiles(event.dataTransfer.files);
    });
  }

  launch.addEventListener("click", function () { setOpen(true); });
  close.addEventListener("click", function () {
    setOpen(false);
    launch.focus();
  });
  plus.addEventListener("click", function () {
    if (files.length >= limit) {
      showNote("10 images is the limit.");
      return;
    }
    fileInput.click();
  });
  fileInput.addEventListener("change", function () {
    addFiles(fileInput.files);
    fileInput.value = "";
  });
  modes.forEach(function (input) {
    input.addEventListener("change", applyMode);
  });
  area.addEventListener("input", function () {
    showNote("");
    syncSend();
    grow();
  });
  form.addEventListener("mousedown", function (event) {
    if (event.target.closest("button, label, a, input")) return;
    var typing = event.target === area && document.activeElement === area && !form.classList.contains("is-thinking");
    if (typing) return;
    if (!area.value.trim() && !files.length && proposal.hidden && !form.classList.contains("is-thinking")) return;
    event.preventDefault();
    clearBox();
    if (event.target === area) area.focus();
  });
  proposal.addEventListener("click", function () {
    clearBox();
    area.focus();
  });
  area.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (!send.disabled) form.requestSubmit();
    }
  });
  area.addEventListener("paste", function (event) {
    var pasted = event.clipboardData && event.clipboardData.files;
    if (!pasted || !pasted.length) return;
    var images = Array.prototype.filter.call(pasted, function (file) {
      return file.type.indexOf("image/") === 0;
    });
    if (!images.length) return;
    event.preventDefault();
    addFiles(images);
  });
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (send.disabled || form.classList.contains("is-thinking")) return;
    var transfer = new DataTransfer();
    files.forEach(function (file) { transfer.items.add(file); });
    fileInput.files = transfer.files;
    var body = new FormData(form);
    var mine = ++ticket;
    var controller = new AbortController();
    inflight = controller;
    setThinking(true);
    fetch("/universe", {
      method: "POST",
      body: body,
      headers: { Accept: "application/json" },
      signal: controller.signal
    }).then(function (response) {
      return response.json().then(function (data) {
        return { ok: response.ok, data: data };
      });
    }).then(function (result) {
      if (mine !== ticket) return;
      inflight = null;
      setThinking(false);
      if (!result.ok || !result.data || !result.data.ok) {
        showNote((result.data && result.data.error) || "That could not be read.");
        return;
      }
      if (result.data.mode === "plan") {
        showProposal(result.data);
        area.blur();
        return;
      }
      showNote(result.data.message || "");
    }).catch(function (error) {
      if (mine !== ticket || (error && error.name === "AbortError")) return;
      inflight = null;
      setThinking(false);
      showNote("That could not be sent.");
    });
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape" || !root.classList.contains("is-open")) return;
    setOpen(false);
    launch.focus();
  });
  watchDrop(launch);
  watchDrop(form);
  applyMode();
  syncSend();
})();
