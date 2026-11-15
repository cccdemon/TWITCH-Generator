(function () {
  const $ = (i) => document.getElementById(i);
  const DATA = "/opt/twitch-generator/data";
  const HIST = 300;                                   // 300 × 2 s = 10 min
  const H = {}; ["busy","tj","tm","tc","cpu","pw","fan","vram","ram","gen","pre","wh","vis","calls"].forEach(k => H[k] = []);
  const STAGES = [["download","Download"],["extract","Extrahieren"],["signals","Signale"],["analyze","Transkript"],["refine","Auswahl"],["render","Schneiden"],["upload","Upload"]];
  let dev = null, hw = null, cpuhw = null, powerCap = null, vramTotal = null, ncpu = 1, lastCpu = null, tickN = 0, gpuName = "";
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

  const read = (p) => cockpit.file(p).read().then(s => (s == null ? null : s.trim())).catch(() => null);
  const readJson = async (p) => { const s = await read(p); try { return s ? JSON.parse(s) : null; } catch (e) { return null; } };
  const num = async (p, div = 1) => { const v = await read(p); return v == null || v === "" ? null : Number(v) / div; };
  const cls = (t, w, b) => t == null ? "" : t >= b ? "bad" : t >= w ? "warn" : "ok";
  const clsLow = (t, w, b) => t == null ? "" : t <= b ? "bad" : t <= w ? "warn" : "ok";
  const push = (k, v) => { H[k].push(v); if (H[k].length > HIST) H[k].shift(); };
  const dur = (s) => { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return h ? `${h} h ${m} min` : m ? `${m} min` : `${s} s`; };
  const k = (n) => n >= 1e6 ? (n / 1e6).toFixed(2) + " M" : n >= 1e3 ? (n / 1e3).toFixed(1) + " k" : String(n);
  const f1 = (n) => n == null ? "–" : Number(n).toFixed(1);
  const f0 = (n) => n == null ? "–" : Math.round(n);
  const val = (id, v, c) => { const el = $(id); if (el.dataset.unit == null) { const s = el.querySelector("small"); el.dataset.unit = s ? s.textContent : ""; }
    el.innerHTML = `<span class="${c || ""}">${v}</span>` + (el.dataset.unit ? `<small>${el.dataset.unit}</small>` : ""); };
  const note = (id, t) => { $(id).innerHTML = t; };

  async function discover() {
    const out = await cockpit.spawn(["sh", "-c",
      "for d in /sys/class/drm/card*/device; do [ -f $d/gpu_busy_percent ] && echo $d && ls -d $d/hwmon/hwmon* 2>/dev/null | head -1; done; " +
      "for h in /sys/class/hwmon/hwmon*; do n=$(cat $h/name 2>/dev/null); case $n in k10temp|zenpower|coretemp) echo CPU:$h;; esac; done; nproc | sed s/^/NCPU:/; " +
      "lspci -d ::0300 2>/dev/null | sed -n 's/.*\\[\\(Radeon[^]]*\\)\\].*/GPU:\\1/p; s/.*VGA compatible controller: \\(.*\\)/GPU:\\1/p' | head -1"], { err: "ignore" });
    for (const line of out.trim().split("\n")) {
      if (line.startsWith("CPU:")) cpuhw = line.slice(4);
      else if (line.startsWith("NCPU:")) ncpu = +line.slice(5) || 1;
      else if (line.startsWith("GPU:")) gpuName = line.slice(4);
      else if (line.includes("/hwmon/")) hw = line;
      else if (line.startsWith("/sys/")) dev = line;
    }
    if (dev) { powerCap = hw ? await num(hw + "/power1_cap", 1e6) : null; vramTotal = await num(dev + "/mem_info_vram_total", 1e9); }
    $("g-cap").textContent = gpuName || (dev ? "AMD-Grafikkarte" : "Keine Grafikkarte gefunden");
    $("lead").innerHTML = `<b>${location.hostname || "clipbox"}</b>, ${ncpu} CPU-Threads${vramTotal ? `, ${vramTotal.toFixed(0)} GB Grafikspeicher` : ""}. Werte alle 2 Sekunden aus /sys, Pipeline aus ${DATA}.<span id="err"></span>`;
  }

  async function cpuPct() {
    const s = await read("/proc/stat"); if (!s) return null;
    const f = s.split("\n")[0].split(/\s+/).slice(1).map(Number);
    const idle = f[3] + (f[4] || 0), total = f.reduce((a, b) => a + b, 0);
    let pct = null;
    if (lastCpu) { const dt = total - lastCpu.total, di = idle - lastCpu.idle; pct = dt > 0 ? Math.round(100 * (1 - di / dt)) : null; }
    lastCpu = { total, idle }; return pct;
  }

  function gauge(busy, tj) {
    const r = 50, circ = 2 * Math.PI * r, p = Math.max(0, Math.min(100, busy || 0));
    const ring = $("ring");
    ring.setAttribute("stroke-dasharray", `${circ * p / 100} ${circ}`);
    ring.setAttribute("stroke-linecap", p > 1 ? "round" : "butt");   // no stray dot at 0 %
    ring.style.stroke = css("--blue");
    $("g-val").textContent = busy == null ? "–" : `${busy}`;
    const t = $("g-temp"); t.textContent = tj == null ? "" : `${Math.round(tj)} °C`; t.setAttribute("fill", css(tj == null ? "--mut" : tj >= 105 ? "--red" : tj >= 90 ? "--gold" : "--green"));
  }

  async function hardware() {
    const [busy, used, te, tj, tm, pw, fan, sclk, mclk] = dev ? await Promise.all([
      num(dev + "/gpu_busy_percent"), num(dev + "/mem_info_vram_used", 1e9),
      hw ? num(hw + "/temp1_input", 1000) : null, hw ? num(hw + "/temp2_input", 1000) : null, hw ? num(hw + "/temp3_input", 1000) : null,
      hw ? num(hw + "/power1_average", 1e6) : null, hw ? num(hw + "/fan1_input") : null, hw ? num(hw + "/freq1_input", 1e6) : null, hw ? num(hw + "/freq2_input", 1e6) : null,
    ]) : [];
    const tc = cpuhw ? await num(cpuhw + "/temp1_input", 1000) : null;
    const la = await read("/proc/loadavg"); const mi = await read("/proc/meminfo"); const cp = await cpuPct();
    gauge(busy, tj);
    val("t-busy", f0(busy)); note("t-busy-n", sclk ? `Takt ${Math.round(sclk)} MHz, Speicher ${f0(mclk)} MHz` : "");
    val("t-tj", f0(tj), cls(tj, 90, 105)); note("t-tj-n", te != null ? `Edge ${f0(te)} °C` : "");
    val("t-tm", f0(tm), cls(tm, 95, 105)); note("t-tm-n", tm != null ? "GDDR6, unkritisch bis 105 °C" : "");
    val("t-pw", f0(pw)); note("t-pw-n", powerCap ? `Limit ${f0(powerCap)} W` : "");
    val("t-vram", f1(used)); note("t-vram-n", vramTotal ? `von ${vramTotal.toFixed(1)} GB belegt` : "");
    val("t-fan", f0(fan)); note("t-fan-n", fan != null ? (fan === 0 ? "steht, passiv gekühlt" : "") : "kein Lüftersensor");
    val("t-cpu", f0(cp)); note("t-cpu-n", la ? `Load ${la.split(" ").slice(0, 3).join(" / ")} auf ${ncpu} Threads` : "");
    val("t-tc", f0(tc), cls(tc, 80, 90)); note("t-tc-n", tc != null ? "Tctl" : "kein Sensor");
    let ramUsed = null;
    if (mi) {
      const kv = {}; mi.split("\n").forEach(l => { const m = l.match(/^(\w+):\s+(\d+)/); if (m) kv[m[1]] = +m[2]; });
      const tot = kv.MemTotal / 1e6; ramUsed = (kv.MemTotal - kv.MemAvailable) / 1e6;
      val("t-ram", f1(ramUsed)); note("t-ram-n", `von ${tot.toFixed(1)} GB belegt`);
    }
    push("busy", busy); push("tj", tj); push("tm", tm); push("tc", tc); push("cpu", cp); push("pw", pw); push("fan", fan); push("vram", used); push("ram", ramUsed);
  }

  async function disk() {
    if (tickN % 15 !== 0) return;                      // every 30 s
    try {
      const out = await cockpit.spawn(["df", "-B1", "--output=size,avail,target", "/"], { err: "ignore" });
      const l = out.trim().split("\n").pop().trim().split(/\s+/); const size = +l[0], avail = +l[1], used = size - avail;
      val("t-disk", (avail / 1e9).toFixed(0), clsLow(100 * avail / size, 15, 5)); note("t-disk-n", `${(used / 1e9).toFixed(0)} von ${(size / 1e9).toFixed(0)} GB belegt`);
    } catch (e) {}
    try {
      const out = await cockpit.spawn(["sh", "-c", `du -sb ${DATA} 2>/dev/null | cut -f1; ls -d ${DATA}/*/ 2>/dev/null | wc -l; ls ${DATA}/*/clips/*.mp4 2>/dev/null | wc -l`], { err: "ignore" });
      const [bytes, vods, clips] = out.trim().split("\n");
      val("t-data", ((+bytes || 0) / 1e9).toFixed(1)); note("t-data-n", `${vods} VODs, ${clips} fertige Clips`);
    } catch (e) {}
  }

  async function pipeline() {
    let jobs = await readJson(DATA + "/jobs.json");
    if (jobs && !Array.isArray(jobs)) jobs = jobs.jobs || Object.values(jobs);
    jobs = jobs || [];
    const active = jobs.filter(j => j.status === "running" || j.status === "queued");
    const job = active[active.length - 1] || jobs[jobs.length - 1];
    if (!job) { $("j-title").textContent = "Noch kein Job gestartet"; $("j-status").textContent = "bereit"; return; }
    const vid = (String(job.vod).match(/(\d{6,})/) || [])[1];
    const [info, m, run] = await Promise.all([vid ? readJson(`${DATA}/${vid}/info.json`) : null, vid ? readJson(`${DATA}/${vid}/metrics.json`) : null, vid ? readJson(`${DATA}/${vid}/run.json`) : null]);
    const now = Date.now() / 1000, running = job.status === "running";
    $("j-title").innerHTML = (info ? `${info.streamer}: ${info.title}` : job.vod) + ` <a href="${location.port === "9090" ? `http://${location.hostname}:9443` : `${location.origin}/vod`}/jobs/${job.id}" target="_blank" style="font-weight:400;font-size:13px;margin-left:8px">Job ${job.id}</a>`;
    const st = $("j-status"); st.className = "status " + job.status;
    st.textContent = { running: "läuft", queued: "wartet", done: "fertig", error: "Fehler", interrupted: "unterbrochen", cancelled: "abgebrochen" }[job.status] || job.status;
    if (!m) { $("timeline").innerHTML = ""; return; }

    const cur = m.stage, stages = m.stages || {};
    const total = Math.max(1, (running ? now : (m.updated || now)) - (m.started || now));
    $("timeline").innerHTML = STAGES.map(([key, label]) => {
      const s = stages[key]; const end = s ? (s.end || (running ? now : m.updated)) : null;
      const c = key === cur ? "now" : (s && s.end) ? "done" : "";
      const title = s ? `${label}: ${dur(end - s.start)}` : `${label}: ausstehend`;
      // finished/current stages share the width in proportion to their duration;
      // pending stages keep their natural label width so nothing gets clipped
      const style = s ? `flex:${Math.max(8, 100 * (end - s.start) / total).toFixed(1)} 1 0` : "flex:0 0 auto;padding:0 10px";
      return `<div class="seg ${c}" style="${style}" title="${title}"><span>${label}${s ? " " + dur(end - s.start) : ""}</span></div>`;
    }).join("");
    const vodS = run ? run.vod_seconds : null, elapsed = total;
    $("f-vod").textContent = vodS ? dur(vodS) : "–";
    $("f-elapsed").textContent = dur(elapsed);
    let eta = "–";
    if (cur === "done") eta = "fertig"; else if (run && run.eta_seconds && running) eta = `≈ ${dur(Math.max(0, run.eta_seconds - (now - run.started)))}`;
    $("f-eta").textContent = eta;
    $("f-factor").textContent = vodS ? `${(elapsed / vodS).toFixed(2)} × Stream-Länge` : "–";

    const l = m.llm || {}, last = l.last;
    if (last && last.gen_tok_s != null) {
      val("t-gen", f1(last.gen_tok_s), clsLow(last.gen_tok_s, 15, 6)); note("t-gen-n", `Ø ${f1(l.gen_tok_s_avg)} tok/s, zuletzt ${(last.model || "").replace("ollama/", "")}`);
    } else { val("t-gen", "–"); note("t-gen-n", "noch kein Modellaufruf"); }
    val("t-pre", last && last.prefill_tok_s ? f0(last.prefill_tok_s) : "–"); note("t-pre-n", last ? `letzter Prompt ${k(last.prompt_tokens)} Tokens in ${last.wall_s} s` : "");
    val("t-tok", k(l.input_tokens || 0)); note("t-tok-n", `rein, ${k(l.output_tokens || 0)} raus, ${l.calls || 0} Aufrufe${l.usd ? `, $${l.usd.toFixed(2)} API` : ", $0 lokal"}`);
    const w = m.whisper || {};
    if (w.chunks_total) { val("t-wh", `${w.chunks_done}/${w.chunks_total}`, ""); note("t-wh-n", w.factor != null ? `${w.factor} × Echtzeit, ${w.model || ""}` : `${w.backend || ""} ${w.model || ""}`); }
    else { val("t-wh", "–"); note("t-wh-n", "Transkript aus dem Cache"); }
    const v = m.vision || {};
    if (v.frames != null) { val("t-vis", `${v.scored ?? 0}/${v.frames}`); note("t-vis-n", `Frames bewertet, ${v.hot ?? 0} auffällig` + (v.bursts != null ? `, ${v.confirmed}/${v.bursts} Bursts bestätigt` : "")); }
    else { val("t-vis", running && cur === "signals" ? "läuft" : "–"); note("t-vis-n", running && cur === "signals" ? "Triage in Arbeit" : ""); }
    const r = m.render || {};
    val("t-clips", r.total ? `${r.done}/${r.total}` : "–"); note("t-clips-n", r.total ? "" : "noch nicht geschnitten");
    if (tickN % 5 === 0) { push("gen", last ? last.gen_tok_s : null); push("pre", last && last.prefill_tok_s ? last.prefill_tok_s / 10 : null); push("wh", w.factor ?? null); push("vis", v.scored ?? null); push("calls", l.calls ?? null); }
    const bm = l.by_model || {};
    $("models").innerHTML = Object.keys(bm).length
      ? Object.entries(bm).map(([n, x]) => `<tr><td>${n.replace("ollama/", "")}</td><td class="num">${x.calls}</td><td class="num">${k(x.input_tokens)}</td><td class="num">${k(x.output_tokens)}</td></tr>`).join("")
      : `<tr><td colspan="4" style="color:var(--mut)">Noch keine Modellaufrufe</td></tr>`;
  }

  async function ollama() {
    try {
      const d = JSON.parse(await cockpit.http(11434).get("/api/ps")); const g = (d.models || [])[0];
      if (g) { const pct = g.size ? Math.round(100 * (g.size_vram || 0) / g.size) : null;
        $("f-model").innerHTML = `${g.name} <span class="${clsLow(pct, 99, 60)}" style="font-weight:500;font-size:13px">${pct}% im VRAM</span>`;
      } else $("f-model").textContent = "keins geladen";
    } catch (e) { $("f-model").textContent = "Ollama nicht erreichbar"; }
  }

  function spark(cv, data, color, lo, hi) {
    const dpr = window.devicePixelRatio || 1, W = cv.width = cv.clientWidth * dpr, Hh = cv.height = cv.clientHeight * dpr;
    const ctx = cv.getContext("2d"); ctx.clearRect(0, 0, W, Hh);
    const vals = data.filter(v => v != null); if (!vals.length) return;
    if (lo == null) lo = Math.min(...vals); if (hi == null) hi = Math.max(...vals); if (hi - lo < 1e-9) { hi = lo + 1; lo = lo - 1; }
    ctx.strokeStyle = color; ctx.lineWidth = 1.5 * dpr; ctx.lineJoin = "round"; ctx.beginPath(); let started = false;
    data.forEach((v, i) => { if (v == null) { started = false; return; } const x = W * i / (HIST - 1), y = Hh - 2 * dpr - (Hh - 4 * dpr) * (v - lo) / (hi - lo); if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y); });
    ctx.stroke();
    const grad = ctx.createLinearGradient(0, 0, 0, Hh); grad.addColorStop(0, color + "33"); grad.addColorStop(1, color + "00");
    ctx.lineTo(W * (data.length - 1) / (HIST - 1), Hh); ctx.lineTo(W * data.findIndex(v => v != null) / (HIST - 1), Hh); ctx.closePath(); ctx.fillStyle = grad; ctx.fill();
  }
  function chart(cv, series, lo, hi, unitRight) {
    const dpr = window.devicePixelRatio || 1, W = cv.width = cv.clientWidth * dpr, Hh = cv.height = cv.clientHeight * dpr, pad = 28 * dpr;
    const ctx = cv.getContext("2d"); ctx.clearRect(0, 0, W, Hh);
    ctx.font = `${11 * dpr}px RedHatText, system-ui, sans-serif`; ctx.fillStyle = css("--mut"); ctx.strokeStyle = css("--track"); ctx.lineWidth = 1;
    for (const f of [0, 0.25, 0.5, 0.75, 1]) { const y = Hh - 4 * dpr - (Hh - 8 * dpr) * f; ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(W, y); ctx.stroke(); ctx.fillText(String(Math.round(lo + (hi - lo) * f)), 0, y + 4 * dpr); }
    for (const [data, color] of series) {
      ctx.strokeStyle = color; ctx.lineWidth = 2 * dpr; ctx.lineJoin = "round"; ctx.beginPath(); let started = false;
      data.forEach((v, i) => { if (v == null) { started = false; return; } const x = pad + (W - pad) * i / (HIST - 1), y = Hh - 4 * dpr - (Hh - 8 * dpr) * (Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo); if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y); });
      ctx.stroke();
    }
  }
  const SPARK = { busy: ["--blue", 0, 100], tj: ["--orange", 30, 110], tm: ["--red", 30, 110], tc: ["--green", 30, 100], cpu: ["--purple", 0, 100], pw: ["--blue", 0, null], fan: ["--purple", 0, null], vram: ["--blue", 0, null], ram: ["--blue", 0, null], gen: ["--blue", 0, null], pre: ["--orange", 0, null], wh: ["--green", 0, null], vis: ["--blue", 0, null], calls: ["--blue", 0, null] };
  function drawAll() {
    document.querySelectorAll("canvas[data-spark]").forEach(cv => { const key = cv.dataset.spark; if (!SPARK[key]) return; const [c, lo, hi] = SPARK[key]; spark(cv, H[key], css(c), lo, hi == null ? (key === "vram" ? vramTotal : key === "pw" ? powerCap : null) : hi); });
    chart($("c1"), [[H.busy, css("--blue")], [H.tj, css("--orange")], [H.tm, css("--red")], [H.tc, css("--green")], [H.cpu, css("--purple")]], 0, 110);
    chart($("c3"), [[H.gen, css("--blue")], [H.pre, css("--orange")], [H.wh, css("--green")]], 0, 100);
  }

  async function tick() {
    try {
      if (!dev && !cpuhw) await discover();
      await Promise.all([hardware(), pipeline(), ollama(), disk()]);
      drawAll(); $("err").textContent = "";
    } catch (e) { $("err").textContent = ` Fehler: ${e.message || e}`; }
    tickN++; setTimeout(tick, 2000);
  }
  tick();
})();
