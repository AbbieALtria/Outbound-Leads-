// Lead status buttons + notes autosave + pull button polling.

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `Request failed (${resp.status})`);
  return data;
}

// ---- geo: cascade the State/Province dropdown from the chosen Country ----
(function initGeo() {
  const country = document.getElementById("pull-country");
  const state = document.getElementById("pull-state");
  if (!country || !state) return;
  let statesByCountry = {};
  try { statesByCountry = JSON.parse(country.dataset.states || "{}"); } catch {}
  const lastState = country.dataset.lastState || "";
  const fill = () => {
    const list = statesByCountry[country.value] || [];
    state.innerHTML = "";
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = list.length ? "State/Province" : "(use City)";
    state.appendChild(blank);
    list.forEach((s) => {
      const o = document.createElement("option");
      o.value = s;
      o.textContent = s;
      if (s === lastState) o.selected = true;
      state.appendChild(o);
    });
    state.disabled = list.length === 0;
  };
  fill();
  country.addEventListener("change", fill);
})();

// ---- city: "+ new city" reveals a text box; otherwise pick from the dropdown ----
function currentCity() {
  const sel = document.getElementById("pull-city");
  const box = document.getElementById("pull-city-new");
  if (sel && sel.value === "__new__") return box && box.value ? box.value.trim() : "";
  return sel ? sel.value : "";
}
(function initCity() {
  const sel = document.getElementById("pull-city");
  const box = document.getElementById("pull-city-new");
  if (!sel || !box) return;
  sel.addEventListener("change", () => {
    const isNew = sel.value === "__new__";
    box.hidden = !isNew;
    if (isNew) box.focus();
  });
})();

// ---- status buttons ----
document.querySelectorAll("tr[data-lead-id]").forEach((row) => {
  const leadId = row.dataset.leadId;

  row.querySelectorAll(".sbtn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      // Clicking the active button toggles the lead back to "new".
      const newStatus = btn.classList.contains("on") ? "new" : btn.dataset.status;
      try {
        await postJSON(`/api/leads/${leadId}/status`, { status: newStatus });
      } catch (e) {
        alert(e.message);
        return;
      }
      row.querySelectorAll(".sbtn").forEach((b) => b.classList.remove("on"));
      if (newStatus !== "new") btn.classList.add("on");
      row.className = `status-${newStatus}`;
    });
  });

  const notes = row.querySelector(".notes");
  if (notes) {
    let last = notes.value;
    const save = async () => {
      if (notes.value === last) return;
      try {
        await postJSON(`/api/leads/${leadId}/notes`, { notes: notes.value });
        last = notes.value;
        notes.classList.add("saved");
        setTimeout(() => notes.classList.remove("saved"), 1200);
      } catch (e) {
        alert(e.message);
      }
    };
    notes.addEventListener("blur", save);
    notes.addEventListener("change", save);
    notes.addEventListener("keydown", (e) => {
      if (e.key === "Enter") notes.blur();
    });
  }
});

// ---- pull button ----
const pullBtn = document.getElementById("pull-btn");
if (pullBtn) {
  const progress = document.getElementById("pull-progress");
  const progressText = document.getElementById("pull-progress-text");
  const result = document.getElementById("pull-result");
  const stopBtn = document.getElementById("stop-btn");

  const clearBtn = document.getElementById("clear-btn");
  let pollTimer = null;

  const setRunning = (running) => {
    pullBtn.disabled = running;
    progress.hidden = !running;
    if (stopBtn) stopBtn.hidden = !running;
  };

  const clearUI = () => {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    setRunning(false);
    progress.hidden = true;
    result.hidden = true;
    progressText.textContent = "";
  };

  const poll = async () => {
    let run;
    try {
      run = await (await fetch("/api/pull/status")).json();
    } catch {
      pollTimer = setTimeout(poll, 3000);
      return;
    }
    if (run.status === "running") {
      const stopping = run.cancel ? " — stopping…" : "";
      progressText.textContent =
        `Pulling ${run.industry} leads… ${run.added}/${run.target} added` +
        (run.current_city ? ` — ${run.current_city}` : "") + stopping;
      pollTimer = setTimeout(poll, 2000);
    } else {
      // Any non-running status ends the progress UI — it can never hang here.
      setRunning(false);
      const ok = run.status === "done" || run.status === "cancelled";
      if (run.status === "none") {
        result.hidden = true;
      } else {
        result.hidden = false;
        result.className = "pull-result " + (ok ? "ok" : "err");
        result.textContent = run.message || run.status;
      }
      if (ok && run.added > 0) setTimeout(() => location.reload(), 1500);
    }
  };

  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      clearUI();
      // Reset filters/selection back to the default dashboard view.
      if (location.search) location.href = location.pathname;
    });
  }

  if (stopBtn) {
    stopBtn.addEventListener("click", async () => {
      stopBtn.disabled = true;
      progressText.textContent = "Stopping after the current query…";
      try {
        await postJSON("/api/pull/cancel", {});
      } catch (e) {
        stopBtn.disabled = false;
        alert(e.message);
      }
    });
  }

  pullBtn.addEventListener("click", async () => {
    const industry = document.getElementById("pull-industry").value;
    if (!industry) {
      result.hidden = false;
      result.className = "pull-result err";
      result.textContent = "Choose an industry to pull.";
      return;
    }
    result.hidden = true;
    if (stopBtn) stopBtn.disabled = false;
    setRunning(true);
    progressText.textContent = "Starting…";
    try {
      await postJSON("/api/pull", {
        industries: [industry],
        target: document.getElementById("pull-target").value,
        location: {
          country: (document.getElementById("pull-country") || {}).value || "",
          state: (document.getElementById("pull-state") || {}).value || "",
          city: currentCity(),
        },
      });
      poll();
    } catch (e) {
      setRunning(false);
      result.hidden = false;
      result.className = "pull-result err";
      result.textContent = e.message;
    }
  });

  // If a pull is genuinely running (e.g. page was refreshed mid-pull), resume the
  // progress UI. Otherwise poll() leaves everything hidden — no stuck "Starting…".
  (async () => {
    try {
      const run = await (await fetch("/api/pull/status")).json();
      if (run.status === "running") {
        setRunning(true);
        poll();
      }
    } catch {}
  })();
}

// ---- verify phones button ----
const verifyBtn = document.getElementById("verify-btn");
if (verifyBtn) {
  const vresult = document.getElementById("verify-result");

  const pollVerify = async () => {
    let s;
    try {
      s = await (await fetch("/api/verify_phones/status")).json();
    } catch {
      setTimeout(pollVerify, 3000);
      return;
    }
    if (s.running) {
      setTimeout(pollVerify, 2000);
    } else {
      verifyBtn.disabled = false;
      verifyBtn.textContent = "Verify phones";
      vresult.className = "pull-result ok";
      vresult.textContent = "Phone verification finished.";
      setTimeout(() => location.reload(), 1200);
    }
  };

  verifyBtn.addEventListener("click", async () => {
    if (!confirm("Validate the not-yet-checked phone numbers in this view via " +
                 "Outscraper? This spends credits (one per number).")) {
      return;
    }
    verifyBtn.disabled = true;
    verifyBtn.textContent = "Verifying…";
    vresult.hidden = false;
    vresult.className = "pull-result";
    vresult.textContent = "Checking phone numbers…";
    try {
      const r = await postJSON("/api/verify_phones", {
        run_id: verifyBtn.dataset.run_id || "",
        status: verifyBtn.dataset.status || "",
        only_unvalidated: true,
      });
      vresult.textContent = `Verifying ${r.count} phone numbers…`;
      pollVerify();
    } catch (e) {
      verifyBtn.disabled = false;
      verifyBtn.textContent = "Verify phones";
      vresult.className = "pull-result err";
      vresult.textContent = e.message;
    }
  });
}
