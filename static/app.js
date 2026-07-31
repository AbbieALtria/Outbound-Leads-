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

// ---- geo cascade: Country -> State -> City (seeded + previously-pulled) ----
function currentCity() {
  const sel = document.getElementById("pull-city");
  const box = document.getElementById("pull-city-new");
  if (sel && sel.value === "__new__") return box && box.value ? box.value.trim() : "";
  return sel ? sel.value : "";
}
(function initGeo() {
  const country = document.getElementById("pull-country");
  const state = document.getElementById("pull-state");
  const city = document.getElementById("pull-city");
  const cityBox = document.getElementById("pull-city-new");
  if (!country || !state || !city) return;
  const parse = (el, attr) => { try { return JSON.parse(el.dataset[attr] || "{}"); } catch { return {}; } };
  const statesByCountry = parse(country, "states");
  const citiesByState = parse(city, "cities");
  const knownByState = parse(city, "known");
  const lastState = country.dataset.lastState || "";
  const lastCity = city.dataset.lastCity || "";

  const opt = (value, label, selected) => {
    const o = document.createElement("option");
    o.value = value; o.textContent = label;
    if (selected) o.selected = true;
    return o;
  };

  const fillCities = () => {
    const seeded = citiesByState[state.value] || [];
    const known = knownByState[state.value] || [];
    const merged = [...new Set([...seeded, ...known])].sort((a, b) => a.localeCompare(b));
    city.innerHTML = "";
    city.appendChild(opt("", "City…"));
    let matched = false;
    merged.forEach((c) => {
      const sel = c === lastCity;
      if (sel) matched = true;
      city.appendChild(opt(c, c, sel));
    });
    // last-used city that isn't in the list (e.g. a typed one)
    if (lastCity && !matched) city.appendChild(opt(lastCity, lastCity, true));
    city.appendChild(opt("__new__", "＋ new city…"));
    cityBox.hidden = true;
  };

  const fillStates = () => {
    const list = statesByCountry[country.value] || [];
    state.innerHTML = "";
    state.appendChild(opt("", list.length ? "State/Province" : "(use City)"));
    list.forEach((s) => state.appendChild(opt(s, s, s === lastState)));
    state.disabled = list.length === 0;
    fillCities();
  };

  country.addEventListener("change", fillStates);
  state.addEventListener("change", fillCities);
  city.addEventListener("change", () => {
    const isNew = city.value === "__new__";
    cityBox.hidden = !isNew;
    if (isNew) cityBox.focus();
  });
  fillStates();
})();

// ---- multi-city sweep picker: Province checklist -> City checklist ----
// Same data + cascade pattern as the single-city dropdown above; no free text.
(function initSweep() {
  const country = document.getElementById("pull-country");
  const provWrap = document.getElementById("sweep-provinces");
  const cityWrap = document.getElementById("sweep-cities");
  const addCity = document.getElementById("sweep-add-city");
  const addProv = document.getElementById("sweep-add-prov");
  const addBtn = document.getElementById("sweep-add-btn");
  const cityEl = document.getElementById("pull-city");
  if (!country || !provWrap || !cityWrap || !cityEl) return;
  const parse = (el, attr) => { try { return JSON.parse(el.dataset[attr] || "{}"); } catch { return {}; } };
  const statesByCountry = parse(country, "states");
  const citiesByState = parse(cityEl, "cities");
  const knownByState = parse(cityEl, "known");
  const custom = {};                       // {province: [added cities]}
  const selected = new Set();              // keys "province\tcity" — survives rebuilds
  const key = (p, c) => p + "\t" + c;

  const checkedProvinces = () =>
    [...provWrap.querySelectorAll("input:checked")].map((i) => i.value);

  const cityChk = (p, c) => {
    const lbl = document.createElement("label"); lbl.className = "chk";
    const inp = document.createElement("input"); inp.type = "checkbox";
    inp.dataset.prov = p; inp.dataset.city = c;
    inp.checked = selected.has(key(p, c));
    inp.addEventListener("change", () =>
      inp.checked ? selected.add(key(p, c)) : selected.delete(key(p, c)));
    lbl.appendChild(inp);
    lbl.appendChild(document.createTextNode(" " + c));
    return lbl;
  };

  const fillCities = () => {
    const provs = checkedProvinces();
    // drop any selection whose province is no longer checked
    [...selected].forEach((k) => { if (!provs.includes(k.split("\t")[0])) selected.delete(k); });
    cityWrap.innerHTML = "";
    if (!provs.length) {
      cityWrap.innerHTML = '<span class="dim small">Check a province to list its cities.</span>';
      return;
    }
    provs.forEach((p) => {
      const cities = [...new Set([
        ...(citiesByState[p] || []), ...(knownByState[p] || []), ...(custom[p] || []),
      ])].sort((a, b) => a.localeCompare(b));
      cities.forEach((c) => cityWrap.appendChild(cityChk(p, c)));
    });
  };

  const fillProvinces = () => {
    const list = statesByCountry[country.value] || [];
    provWrap.innerHTML = ""; addProv.innerHTML = "";
    selected.clear();
    list.forEach((s) => {
      const lbl = document.createElement("label"); lbl.className = "chk";
      const inp = document.createElement("input"); inp.type = "checkbox"; inp.value = s;
      inp.addEventListener("change", fillCities);
      lbl.appendChild(inp); lbl.appendChild(document.createTextNode(" " + s));
      provWrap.appendChild(lbl);
      const o = document.createElement("option"); o.value = s; o.textContent = s;
      addProv.appendChild(o);
    });
    if (!list.length) provWrap.innerHTML = '<span class="dim small">No provinces for this country.</span>';
    fillCities();
  };

  if (addBtn) addBtn.addEventListener("click", () => {
    const c = (addCity.value || "").trim(); const p = addProv.value;
    if (!c || !p) return;
    (custom[p] = custom[p] || []).push(c);
    const provCb = [...provWrap.querySelectorAll("input")].find((i) => i.value === p);
    if (provCb && !provCb.checked) provCb.checked = true;
    selected.add(key(p, c));
    addCity.value = "";
    fillCities();
  });

  country.addEventListener("change", fillProvinces);
  fillProvinces();
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

  // Multi-city sweep: the checked city checkboxes (each carries its province).
  // Same {city, state, country} shape the backend already expects.
  const parseLocations = () => {
    const country = (document.getElementById("pull-country") || {}).value || "";
    return [...document.querySelectorAll("#sweep-cities input:checked")].map((cb) => ({
      city: cb.dataset.city, state: cb.dataset.prov, country,
    }));
  };

  pullBtn.addEventListener("click", async () => {
    // Industry + location are always read from the dashboard pickers. A campaign
    // (if selected) only tags the leads; it no longer locks industry/geo. Read the
    // dropdown's current value so whatever is shown is what gets sent.
    const campSel = document.getElementById("pull-campaign");
    const campaignId = (campSel ? campSel.value : "") || pullBtn.dataset.campaign || "";
    const industryEl = document.getElementById("pull-industry");
    const industry = industryEl ? industryEl.value : "";
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
      const payload = { target: document.getElementById("pull-target").value };
      if (industry === "__all__") {
        payload.all_industries = true;      // sweep every industry in the catalog
      } else {
        payload.industries = [industry];
      }
      if (campaignId) payload.campaign_id = Number(campaignId);
      const locations = parseLocations();
      if (locations.length) {
        payload.locations = locations;   // multi-city sweep
      } else {
        payload.location = {
          country: (document.getElementById("pull-country") || {}).value || "",
          state: (document.getElementById("pull-state") || {}).value || "",
          city: currentCity(),
        };
      }
      await postJSON("/api/pull", payload);
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

// ---- show/hide password (eye toggle) ----
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".pw-eye");
  if (!btn) return;
  const input = btn.closest(".pw-wrap")?.querySelector("input");
  if (!input) return;
  const reveal = input.type === "password";
  input.type = reveal ? "text" : "password";
  btn.textContent = reveal ? "🙈" : "👁";
  btn.setAttribute("aria-label", reveal ? "Hide password" : "Show password");
});

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

// ---- enrich contacts button (Apollo) ----
const enrichBtn = document.getElementById("enrich-btn");
if (enrichBtn) {
  const eresult = document.getElementById("enrich-result");

  const pollEnrich = async () => {
    let s;
    try {
      s = await (await fetch("/api/enrich_contacts/status")).json();
    } catch {
      setTimeout(pollEnrich, 3000);
      return;
    }
    if (s.running) {
      setTimeout(pollEnrich, 2000);
    } else {
      enrichBtn.disabled = false;
      enrichBtn.textContent = "Enrich contacts";
      const r = s.last || {};
      if (r.error) {
        eresult.className = "pull-result err";
        eresult.textContent = "Apollo error: " + r.error;
        return;   // don't reload — keep the error visible
      }
      eresult.className = "pull-result ok";
      eresult.textContent =
        `Enriched ${r.enriched || 0} of ${r.checked || 0} Apollo calls — ` +
        `${r.emails || 0} emails, ${r.phones || 0} direct dials. ` +
        `Skipped ${r.skipped_already_enriched || 0} already-enriched (Apollo credits saved). ` +
        `${r.org_calls || 0} company lookups (1 credit each).`;
      setTimeout(() => location.reload(), 1800);
    }
  };

  enrichBtn.addEventListener("click", async () => {
    if (!confirm("Find the decision-maker for this pull's leads (that have a website) " +
                 "via Apollo? Names & titles are free; email/direct-dial reveal spends " +
                 "Apollo credits per lead (set in Settings).")) {
      return;
    }
    enrichBtn.disabled = true;
    enrichBtn.textContent = "Enriching…";
    eresult.hidden = false;
    eresult.className = "pull-result";
    eresult.textContent = "Looking up decision-makers…";
    try {
      const r = await postJSON("/api/enrich_contacts", {
        run_id: enrichBtn.dataset.run_id || "",
        only_missing: true,
      });
      eresult.textContent = `Enriching ${r.count} leads…`;
      pollEnrich();
    } catch (e) {
      enrichBtn.disabled = false;
      enrichBtn.textContent = "Enrich contacts";
      eresult.className = "pull-result err";
      eresult.textContent = e.message;
    }
  });
}
