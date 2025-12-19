const widgetShell = document.getElementById("widget-shell");
const inputEl = document.getElementById("input");
const chatScroll = document.getElementById("chatScroll");
const settingsView = document.getElementById("settings-view");
const runBtn = document.getElementById("run-btn");
const expandBtn = document.getElementById("expand-btn");
const settingsBtn = document.getElementById("settings-btn");
const clearBtn = document.getElementById("clear-btn");
const quitBtn = document.getElementById("quit-btn");
const workDirInput = document.getElementById("work-dir-input");
const workDirChooseBtn = document.getElementById("work-dir-choose");
const statusDot = document.getElementById("statusDot");
const statusText = document.getElementById("clickPickerStatus");
const panelTitle = document.getElementById("panel-title");
const modelOptions = document.getElementById("model-options");
const modelButtons = modelOptions ? Array.from(modelOptions.querySelectorAll(".pill-btn")) : [];
const evidencePanel = document.getElementById("evidence-panel");
const evidenceList = document.getElementById("evidence-list");
const evidenceStatus = document.getElementById("evidence-status");
const evidenceClear = document.getElementById("evidence-clear");
const artifactModal = document.getElementById("artifact-modal");
const artifactBody = document.getElementById("artifact-body");
const artifactClose = artifactModal ? artifactModal.querySelector(".modal-close") : null;
const PROVIDER_LABELS = { deepseek: "DeepSeek", doubao: "Doubao", qwen: "Qwen" };
const PROVIDER_STORAGE_KEY = "pc_assistant_provider";
const modeOptions = document.getElementById("mode-options");
const modeButtons = modeOptions ? Array.from(modeOptions.querySelectorAll(".pill-btn")) : [];
const MODE_STORAGE_KEY = "pc_assistant_mode";
const MODE_LABELS = { execute: "Execute", chat: "Chat" };
const DEFAULT_API_BASE = "http://127.0.0.1:5004";
const API_BASE = (window.api && window.api.backendBaseUrl) || DEFAULT_API_BASE;
const CHAT_TIMEOUT_MS = 45000;
const EVIDENCE_PAYLOAD_LIMIT = 500;
const log = (level, message) => {
    try {
        if (window.api && typeof window.api.log === "function") {
            window.api.log(level, message);
        }
    } catch (err) {
        // ignore logging failures
    }
};

function escapeHTML(str) {
    if (str === null || str === undefined) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

// Hidden but necessary elements for backend logic compatibility
const screenshotMain = document.getElementById("screenshotMain");
const ocrTextEl = document.getElementById("ocrText");
const dryRunToggle = document.getElementById("dry-run-toggle");
const screenshotBtn = document.getElementById("screenshot-btn");
const ocrBtn = document.getElementById("ocr-btn");

let screenshotMeta = null;
let screenshotBase64 = null;
let isSettingsOpen = false;
let currentProvider = "deepseek";
let currentMode = "execute";
let currentStatusState = "idle";
let currentWorkDir = "";
let backendConnectionState = "online"; // online | reconnecting | offline
let healthTimer = null;
let healthController = null;
let healthBackoffMs = 1000;
const HEALTH_BACKOFF_MAX = 15000;
let healthFailures = 0;
let lastRestartAt = 0;
const RESTART_COOLDOWN_MS = 30000;
let evidenceEventSource = null;
let evidenceRequestId = null;

function getApi() {
    if (window.api) return window.api;
    // Fallback: minimal bridge to keep UI usable if preload failed.
    return {
        defaultWorkDir: "",
        run: async (text, ocrText = "", manualClick = null, screenshotMeta = null, dryRun = false, workDir = null, screenshotBase64 = null, provider = currentProvider) => {
            try {
                const res = await fetch(`${API_BASE}/api/ai/run`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        user_text: text,
                        ocr_text: ocrText,
                        manual_click: manualClick,
                        screenshot_meta: screenshotMeta,
                        dry_run: dryRun,
                        work_dir: workDir,
                        screenshot_base64: screenshotBase64,
                        provider: provider || currentProvider,
                    }),
                });
                return await res.json();
            } catch (err) {
                return { error: "fallback_run_failed: " + err };
            }
        },
        exitApp: () => window.close(),
    };
}

function getProviderLabel(provider = currentProvider) {
    const normalized = (provider || "").toLowerCase();
    return PROVIDER_LABELS[normalized] || normalized || "Unknown";
}

function getModeLabel(mode = currentMode) {
    const normalized = (mode || "").toLowerCase();
    return MODE_LABELS[normalized] || normalized || "Unknown";
}

function normalizeProvider(value) {
    return (value || "").toLowerCase();
}

function loadStoredProvider() {
    try {
        const stored = window.localStorage?.getItem(PROVIDER_STORAGE_KEY);
        const normalized = normalizeProvider(stored);
        if (normalized && PROVIDER_LABELS[normalized]) return normalized;
    } catch (err) {
        // Ignore storage errors (e.g., disabled storage)
    }
    return null;
}

function persistProvider(provider) {
    const normalized = normalizeProvider(provider);
    if (!normalized || !PROVIDER_LABELS[normalized]) return;
    try {
        window.localStorage?.setItem(PROVIDER_STORAGE_KEY, normalized);
    } catch (err) {
        // Ignore storage write failures
    }
}

function normalizeMode(value) {
    return (value || "").toLowerCase();
}

function loadStoredMode() {
    try {
        const stored = window.localStorage?.getItem(MODE_STORAGE_KEY);
        const normalized = normalizeMode(stored);
        if (normalized && MODE_LABELS[normalized]) return normalized;
    } catch (err) {
        // Ignore storage errors
    }
    return null;
}

function persistMode(mode) {
    const normalized = normalizeMode(mode);
    if (!normalized || !MODE_LABELS[normalized]) return;
    try {
        window.localStorage?.setItem(MODE_STORAGE_KEY, normalized);
    } catch (err) {
        // Ignore storage write failures
    }
}

function syncProviderButtons(provider) {
    if (!modelButtons || modelButtons.length === 0) return;
    const normalized = normalizeProvider(provider);
    modelButtons.forEach((btn) => {
        const btnProvider = normalizeProvider(btn.dataset.provider);
        if (btnProvider === normalized) btn.classList.add("active");
        else btn.classList.remove("active");
    });
}

function syncModeButtons(mode) {
    if (!modeButtons || modeButtons.length === 0) return;
    const normalized = normalizeMode(mode);
    modeButtons.forEach((btn) => {
        const btnMode = normalizeMode(btn.dataset.mode);
        if (btnMode === normalized) btn.classList.add("active");
        else btn.classList.remove("active");
    });
}

function applyProvider(provider) {
    const normalized = normalizeProvider(provider);
    if (!normalized || !PROVIDER_LABELS[normalized]) return;
    currentProvider = normalized;
    persistProvider(normalized);
    syncProviderButtons(normalized);
    setStatus(currentStatusState);
}

function applyMode(mode) {
    const normalized = normalizeMode(mode);
    if (!normalized || !MODE_LABELS[normalized]) return;
    currentMode = normalized;
    persistMode(normalized);
    syncModeButtons(normalized);
    setStatus(currentStatusState);
}

// Initialize Settings
(function initWorkDir() {
    const api = getApi();
    if (api && api.defaultWorkDir) {
        currentWorkDir = api.defaultWorkDir;
        workDirInput.value = api.defaultWorkDir;
    } else {
        workDirInput.value = "";
    }
})();

(function initProviderSelection() {
    const stored = loadStoredProvider();
    if (stored && PROVIDER_LABELS[stored]) {
        currentProvider = stored;
    }
    syncProviderButtons(currentProvider);
})();

(function initModeSelection() {
    const stored = loadStoredMode();
    if (stored && MODE_LABELS[stored]) {
        currentMode = stored;
    }
    syncModeButtons(currentMode);
})();

// --- UI State Management ---

function toggleExpand(forceState = null) {
    if (forceState !== null) {
        if (forceState) widgetShell.classList.add("expanded");
        else widgetShell.classList.remove("expanded");
    } else {
        widgetShell.classList.toggle("expanded");
    }
}

async function chooseWorkDir() {
    const api = getApi();
    if (!api || typeof api.selectWorkDir !== "function") {
        appendAgentHTML('<div class="bubble" style="color:var(--error-color); background:#FFF5F5;">Folder picker unavailable. Please restart the app.</div>');
        return;
    }
    const previousStatus = currentStatusState;
    const selected = await api.selectWorkDir();
    if (selected) {
        currentWorkDir = selected;
        workDirInput.value = selected;
    }
    setStatus(previousStatus);
}

function toggleSettings() {
    isSettingsOpen = !isSettingsOpen;
    
    // Ensure expanded if opening settings
    if (isSettingsOpen) {
        toggleExpand(true);
        chatScroll.style.display = "none";
        settingsView.classList.add("active");
        settingsBtn.classList.add("active");
        panelTitle.textContent = "Configuration";
    } else {
        chatScroll.style.display = "flex";
        settingsView.classList.remove("active");
        settingsBtn.classList.remove("active");
        panelTitle.textContent = "Assistant Session";
    }
}

function formatStatusLabel(text) {
    const backendSuffix =
        backendConnectionState === "reconnecting"
            ? " | Backend: reconnecting..."
            : backendConnectionState === "offline"
            ? " | Backend: offline"
            : "";
    return `${text}${backendSuffix} | ${getProviderLabel()} | ${getModeLabel()}`;
}

function setStatus(state) {
    currentStatusState = state || "idle";
    statusDot.className = "status-indicator"; // reset
    if (currentStatusState === "busy") {
        statusDot.classList.add("busy");
        statusText.textContent = formatStatusLabel("THINKING...");
        statusText.style.color = "var(--accent-color)";
    } else if (currentStatusState === "success") {
        statusDot.classList.add("active");
        statusText.textContent = formatStatusLabel("DONE");
        statusText.style.color = "var(--success-color)";
    } else if (currentStatusState === "error") {
        statusDot.classList.add("error");
        statusText.textContent = formatStatusLabel("FAILED");
        statusText.style.color = "var(--error-color)";
    } else {
        statusText.textContent = formatStatusLabel("IDLE");
        statusText.style.color = "var(--text-sub)";
    }

    if (backendConnectionState === "offline") {
        statusText.style.color = "var(--error-color)";
    } else if (backendConnectionState === "reconnecting") {
        statusText.style.color = "var(--accent-color)";
    }
}

function scrollToBottom() {
    chatScroll.scrollTop = chatScroll.scrollHeight;
}

function setBackendState(state) {
    const normalized = state || "online";
    if (backendConnectionState === normalized) return;
    backendConnectionState = normalized;
    setStatus(currentStatusState);
}

function scheduleHealthCheck(delayMs = healthBackoffMs) {
    if (healthTimer) clearTimeout(healthTimer);
    healthTimer = setTimeout(runHealthCheck, delayMs);
}

async function runHealthCheck() {
    if (healthController) {
        healthController.abort();
    }
    healthController = new AbortController();
    const ctrl = healthController;
    const timeout = setTimeout(() => ctrl.abort(), 1500);
    try {
        const res = await fetch(`${API_BASE}/`, { signal: ctrl.signal });
        if (res.ok) {
            healthFailures = 0;
            healthBackoffMs = 1000;
            setBackendState("online");
            log("INFO", "Health check OK");
        } else {
            throw new Error(`health http ${res.status}`);
        }
    } catch (err) {
        healthFailures += 1;
        const reconnecting = healthFailures < 3;
        setBackendState(reconnecting ? "reconnecting" : "offline");
        healthBackoffMs = Math.min(healthBackoffMs * 2, HEALTH_BACKOFF_MAX);
        log("WARNING", `Health check failed (${err}); failures=${healthFailures}`);

        const now = Date.now();
        if (
            healthFailures >= 3 &&
            window.api &&
            typeof window.api.restartBackend === "function" &&
            now - lastRestartAt > RESTART_COOLDOWN_MS
        ) {
            lastRestartAt = now;
            // Fire and forget restart; don't await to avoid UI freeze.
            log("WARNING", "Requesting backend restart after repeated failures");
            window.api.restartBackend().catch(() => {});
        }
    } finally {
        clearTimeout(timeout);
        if (ctrl === healthController) {
            healthController = null;
        }
        scheduleHealthCheck();
    }
}

function startHealthMonitor() {
    healthBackoffMs = 1000;
    healthFailures = 0;
    setBackendState("online");
    scheduleHealthCheck(0);
}

// --- Chat Helpers ---

function appendUserMessage(text) {
    const div = document.createElement("div");
    div.className = "bubble user";
    div.textContent = text;
    chatScroll.appendChild(div);
    scrollToBottom();
}

function appendAgentHTML(html) {
    const div = document.createElement("div");
    div.innerHTML = html; // Assume html includes wrapper classes
    chatScroll.appendChild(div);
    scrollToBottom();
    return div;
}

function createScreenshotCard(base64) {
    return `
    <div class="screenshot-card">
        <div class="live-badge">VISION CAPTURED</div>
        <img src="data:image/png;base64,${base64}" />
    </div>`;
}

function createPlanBubble(data) {
    if (data.plan_error) {
        return `<div class="bubble" style="color:#D32F2F; background:#FFEBEE; border:1px solid #FFCDD2;">⚠️ ${data.plan_error}</div>`;
    }
    if (data.plan_status === "error" && data.plan_error) {
        const detail = data.plan_error.detail
            ? `<pre style="background:#0b1021; color:#e6f1ff; padding:8px; border-radius:6px; font-size:12px; overflow:auto; max-height:200px;">${escapeHTML(
                  JSON.stringify(data.plan_error.detail, null, 2)
              )}</pre>`
            : "";
        return `<div class="bubble" style="color:#D32F2F; background:#FFEBEE; border:1px solid #FFCDD2;">
            <div style="font-weight:700;">${escapeHTML(data.plan_error.category || "Plan Error")}</div>
            <div>${escapeHTML(data.plan_error.message || "")}</div>
            ${detail}
        </div>`;
    }
    if (data.plan_status === "awaiting_user" && data.clarification) {
        const c = data.clarification || {};
        const options = Array.isArray(c.options)
            ? c.options
                  .map(
                      (opt) =>
                          `<button class="pill-btn clarify-btn" data-clarify-value="${escapeHTML(opt.value)}" style="margin:4px 6px 0 0;">${escapeHTML(
                              opt.label
                          )}</button>`
                  )
                  .join("")
            : "";
        return `<div class="bubble" style="background:#F0F7FF; color:#0F2744; border:1px solid #cfe2ff;">
            <div style="font-weight:700; margin-bottom:4px;">${escapeHTML(c.question || "Need clarification")}</div>
            ${c.hint ? `<div style="font-size:12px; color:#555; margin-bottom:6px;">${escapeHTML(c.hint)}</div>` : ""}
            <div style="display:flex; flex-wrap:wrap;">${options}</div>
        </div>`;
    }
    
    const planObj = data.plan || data.plan_after_injection || data;
    const steps = planObj.steps || [];
    
    if (steps.length === 0) {
        if (!data.plan_status) {
            return `<div class="bubble">No steps generated.</div>`;
        }
        return "";
    }

    const htmlSteps = steps.map(s => {
        let icon = "⚡️";
        if (s.action.includes('click')) icon = "🖱️";
        if (s.action.includes('type')) icon = "⌨️";
        if (s.action.includes('wait')) icon = "⏳";
        
        let params = JSON.stringify(s.params || {}).replace(/["{}]/g, '');
        if(params.length > 30) params = params.substring(0, 27) + "..";
        
        return `
        <div class="step-item">
            <div class="step-icon">${icon}</div>
            <div class="step-text">${s.action}</div>
            <div class="step-meta">${params}</div>
        </div>`;
    }).join('');
    
    return `<div class="bubble" style="padding:0 16px;">${htmlSteps}</div>`;
}

function createPlanCardWrapper(data) {
    return createPlanBubble(data);
}

function bindClarificationButtons(rootEl = document) {
    const buttons = Array.from(rootEl.querySelectorAll(".clarify-btn"));
    buttons.forEach((btn) => {
        if (btn.dataset.bound === "1") return;
        btn.dataset.bound = "1";
        btn.addEventListener("click", async () => {
            const value = btn.dataset.clarifyValue || btn.textContent || "";
            if (!value) return;
            btn.disabled = true;
            inputEl.value = value;
            setStatus("busy");
            try {
                await handleRun();
            } finally {
                btn.disabled = false;
            }
        });
    });
}

// --- Evidence Stream (Manual checklist near this block)
// Manual check: run a task that emits evidence; verify seq increases, artifact buttons open image/JSON; start another run and old stream closes.
function setEvidenceStatus(text) {
    if (evidenceStatus) evidenceStatus.textContent = text;
}

function clearEvidence() {
    if (evidenceList) evidenceList.innerHTML = "";
}

function stopEvidenceStream() {
    if (evidenceEventSource) {
        try {
            evidenceEventSource.close();
        } catch (err) {
            // ignore
        }
    }
    evidenceEventSource = null;
    evidenceRequestId = null;
    setEvidenceStatus("stopped");
}

function truncatePayload(payload) {
    try {
        const text = JSON.stringify(payload ?? {});
        return text.length > EVIDENCE_PAYLOAD_LIMIT ? `${text.slice(0, EVIDENCE_PAYLOAD_LIMIT)}...` : text;
    } catch (err) {
        return "";
    }
}

function openArtifactModal(requestId, artifact) {
    if (!artifact || !artifactModal || !artifactBody) return;
    artifactBody.textContent = "Loading artifact...";
    artifactModal.style.display = "flex";
    const url = `${API_BASE}/api/artifact/${encodeURIComponent(requestId)}/${encodeURIComponent(artifact.artifact_id)}`;
    if (artifact.kind === "image") {
        const img = document.createElement("img");
        img.src = url;
        img.alt = artifact.artifact_id || "artifact";
        img.onload = () => {
            artifactBody.innerHTML = "";
            artifactBody.appendChild(img);
        };
        img.onerror = () => {
            artifactBody.textContent = "Failed to load image artifact.";
        };
    } else {
        fetch(url)
            .then((res) => res.text())
            .then((txt) => {
                artifactBody.textContent = txt;
            })
            .catch(() => {
                artifactBody.textContent = "Failed to load artifact.";
            });
    }
}

function closeArtifactModal() {
    if (artifactModal) artifactModal.style.display = "none";
    if (artifactBody) artifactBody.textContent = "";
}

function renderEvidenceEvent(ev) {
    if (!evidenceList) return;
    const row = document.createElement("div");
    row.className = "evidence-row";

    const meta = document.createElement("div");
    meta.className = "evidence-meta";
    const parts = [
        `#${ev.seq}`,
        ev.type || "",
        typeof ev.step_index === "number" ? `step ${ev.step_index}` : "",
        typeof ev.attempt === "number" ? `attempt ${ev.attempt}` : "",
    ].filter(Boolean);
    meta.textContent = parts.join(" · ");
    row.appendChild(meta);

    const payload = document.createElement("div");
    payload.className = "evidence-payload";
    payload.textContent = truncatePayload(ev.payload || {});
    row.appendChild(payload);

    if (ev.artifact && ev.artifact.artifact_id) {
        const actions = document.createElement("div");
        actions.className = "evidence-actions";
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn-link";
        btn.textContent = "View artifact";
        btn.addEventListener("click", () => openArtifactModal(ev.request_id, ev.artifact));
        actions.appendChild(btn);
        row.appendChild(actions);
    }

    evidenceList.appendChild(row);
    if (evidencePanel) {
        evidencePanel.scrollTop = evidencePanel.scrollHeight;
    }
}

function startEvidenceStream(requestId) {
    if (!requestId) return;
    if (typeof EventSource === "undefined") {
        setEvidenceStatus("unsupported");
        return;
    }
    if (evidenceEventSource) {
        stopEvidenceStream();
    }
    evidenceRequestId = requestId;
    setEvidenceStatus("connecting");
    const url = `${API_BASE}/api/stream/${encodeURIComponent(requestId)}`;
    try {
        const es = new EventSource(url);
        evidenceEventSource = es;
        es.onopen = () => setEvidenceStatus("connected");
        es.onerror = () => setEvidenceStatus("disconnected");
        es.onmessage = (evt) => {
            try {
                const data = JSON.parse(evt.data);
                renderEvidenceEvent(data);
            } catch (err) {
                // ignore parse errors
            }
        };
    } catch (err) {
        setEvidenceStatus("error");
    }
}

function createExecutionSummaryCard(result) {
    const execution = result.execution || result;
    const summary = execution?.summary || result.summary || result.context?.summary;
    const rewrites = execution?.plan_rewrites || result.plan_rewrites;
    if (!summary && (!rewrites || rewrites.length === 0)) return "";

    const lines = [];
    if (summary?.summary_text) {
        lines.push(summary.summary_text);
    }
    const failures = summary?.failures || [];
    if (failures.length) {
        lines.push(`Issues: ${failures.slice(0, 3).join(" | ")}`);
    }
    if (rewrites && rewrites.length) {
        const rewriteText = rewrites.map(r => `${r.pattern || "plan"}→${r.replacement || "rewrite"}`).join("; ");
        lines.push(`Rewrites: ${rewriteText}`);
    }
    if (!lines.length) return "";

    return `<div class="bubble" style="background:#F0F7FF; color:#0F2744;">${lines.join("<br>")}</div>`;
}

function renderStatusBadge(status) {
    const normalized = (status || "").toLowerCase();
    const palette = {
        success: { bg: "#E6FFFA", fg: "#0F5132" },
        error: { bg: "#FFF5F5", fg: "#842029" },
        unsafe: { bg: "#FFF5F5", fg: "#842029" },
        skipped: { bg: "#F8F9FA", fg: "#6C757D" },
        replanned: { bg: "#E7F1FF", fg: "#0D3B66" },
        awaiting_user: { bg: "#FFF3CD", fg: "#664D03" },
        dry_run: { bg: "#E7F1FF", fg: "#0D3B66" },
    };
    const colors = palette[normalized] || palette["success"];
    const label = normalized || "unknown";
    return `<span style="padding:2px 6px; border-radius:6px; background:${colors.bg}; color:${colors.fg}; font-size:12px;">${escapeHTML(
        label
    )}</span>`;
}

function createEvidenceCard(result) {
    const execution = result.execution || result;
    const logs = Array.isArray(execution?.logs) ? execution.logs : [];
    if (!logs.length) return "";
    const requestId = result.request_id || execution.request_id || "unknown";
    const overall = execution.overall_status || result.overall_status || "unknown";

    const rows = logs
        .map((log) => {
            const attempts = Array.isArray(log.attempts) ? log.attempts : [];
            const isFailure = (log.status || "").toLowerCase() === "error" || (log.status || "").toLowerCase() === "unsafe";
            const reason = log.reason || log.message || "";
            const header = `<div style="display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap;">
                <div style="font-weight:600;">#${log.step_index ?? "?"} · ${escapeHTML(log.action || "unknown")}</div>
                <div style="display:flex; gap:6px; align-items:center;">
                    ${renderStatusBadge(log.status)}
                    ${reason ? `<span style="font-size:12px; color:${isFailure ? "#842029" : "#555"};">${escapeHTML(reason)}</span>` : ""}
                </div>
            </div>`;

            const attemptHtml = attempts
                .map((att) => {
                    const ev = att.evidence || att.verification?.evidence || log.evidence || {};
                    const capturePhase = ev?.capture_phase || "verify";
                    const vReason = att.verification?.reason || att.reason || ev?.reason || "";
                    const vDecision = att.verification?.decision || "";
                    const evidenceJson = escapeHTML(JSON.stringify(ev || {}, null, 2));
                    const expectActual = [];
                    if (ev?.expected) expectActual.push(`<div><strong>Expected</strong>: ${escapeHTML(JSON.stringify(ev.expected))}</div>`);
                    if (ev?.actual) expectActual.push(`<div><strong>Actual</strong>: ${escapeHTML(JSON.stringify(ev.actual))}</div>`);
                    const focusLine =
                        ev?.focus_expected || ev?.focus_actual
                            ? `<div style="font-size:12px; color:#555;">Focus: expected=${escapeHTML(
                                  JSON.stringify(ev.focus_expected || {})
                              )} · actual=${escapeHTML(JSON.stringify(ev.focus_actual || {}))}</div>`
                            : "";
                    const riskLine = ev?.risk
                        ? `<div style="font-size:12px; color:#555;">Risk: ${escapeHTML(ev.risk.level || "")} (${escapeHTML(
                              ev.risk.reason || ""
                          )})</div>`
                        : "";
                    return `<details style="margin-top:8px; border:1px solid #EEE; border-radius:8px; padding:8px;" ${
                        isFailure ? "open" : ""
                    }>
                        <summary style="cursor:pointer; display:flex; justify-content:space-between; align-items:center; gap:8px;">
                            <div style="font-weight:600;">Attempt ${att.attempt ?? "?"}</div>
                            <div style="display:flex; gap:6px; align-items:center;">
                                ${renderStatusBadge(att.status || vDecision || "unknown")}
                                <span style="font-size:12px; color:#555;">${escapeHTML(vReason || att.reason || "")}</span>
                                <span style="font-size:12px; color:#0D3B66;">${escapeHTML(capturePhase)}</span>
                            </div>
                        </summary>
                        ${focusLine}
                        ${riskLine}
                        ${expectActual.join("")}
                        <pre style="background:#0b1021; color:#e6f1ff; padding:8px; border-radius:6px; font-size:12px; overflow:auto; max-height:240px; margin-top:6px;">${evidenceJson}</pre>
                    </details>`;
                })
                .join("");

            // If no attempts but evidence on the log, show it once.
            const logEvidence = !attempts.length && log.evidence ? `<pre style="background:#0b1021; color:#e6f1ff; padding:8px; border-radius:6px; font-size:12px; overflow:auto;">${escapeHTML(
                JSON.stringify(log.evidence, null, 2)
            )}</pre>` : "";

            return `<div style="border:1px solid ${isFailure ? "#f3b8c0" : "#eee"}; background:${isFailure ? "#fff5f5" : "#fafafa"}; border-radius:10px; padding:10px; margin-top:10px;">
                ${header}
                ${attemptHtml || logEvidence || '<div style="font-size:12px; color:#777; margin-top:4px;">No attempt details.</div>'}
            </div>`;
        })
        .join("");

    return `<div class="bubble" style="background:#F8F8F8; color:var(--text-main);">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
            <div style="font-weight:700;">Evidence & Attempts</div>
            <div style="font-size:12px; color:#555;">Request: ${escapeHTML(requestId)} · Status: ${escapeHTML(overall)}</div>
        </div>
        <div style="display:flex; align-items:center; gap:8px; font-size:12px; margin-bottom:6px;">
            <span style="padding:2px 6px; border-radius:6px; background:#eef2ff; color:#312e81;">Use this section to debug failures, focus gates, and consent blocks.</span>
        </div>
        ${rows}
    </div>`;
}

function appendExecutionDetails(result) {
    const execution = result?.execution || result;
    const logs = Array.isArray(execution?.logs) ? execution.logs : [];
    if (!logs.length) return;

    logs.forEach((log) => {
        // 1) UI state warnings
        if (log.warning) {
            appendAgentHTML(
                `<div class="bubble" style="color:#856404; background:#fff3cd; border:1px solid #ffeeba;">⚠️ <strong>无效操作检测</strong><br>界面状态未发生变化 (UI State Unchanged)</div>`
            );
            // Continue to allow other info in the same log (if any)
        }

        if (log.status !== "success") return;

        // 2) Click success feedback
        if (log.action === "click") {
            const method =
                log.method ||
                (typeof log.message === "object" && log.message ? log.message.method : "") ||
                "Unknown";
            appendAgentHTML(
                `<div class="bubble" style="color:#155724; background:#d4edda; border:1px solid #c3e6cb;">🖱️ 点击执行成功 <span style="font-size:0.85em; opacity:0.8; margin-left:4px;">(引擎: ${escapeHTML(
                    method
                )})</span></div>`
            );
        }

        if (log.action === "read_file") {
            const message = log.message || log.result || {};
            const content = message?.content ?? message?.result?.content;
            if (typeof content !== "string") return;
            const truncated = message?.truncated === true || message?.result?.truncated === true;
            const pathLabel = message?.path || log?.params?.path || "file";
            const header = `${pathLabel}${truncated ? " (truncated)" : ""}`;
            appendAgentHTML(
                `<div class="bubble" style="background:#F8F8F8; color:var(--text-main); white-space:pre-wrap;"><div style="font-weight:600; margin-bottom:4px;">${escapeHTML(header)}</div>${escapeHTML(content)}</div>`
            );
        } else if (log.action === "open_file") {
            const message = log.message || log.result || {};
            const pathLabel = message?.path || log?.params?.path || "file";
            const method = message?.method || message?.result?.method;
            appendAgentHTML(
                `<div class="bubble" style="background:#F8F8F8; color:var(--text-main);"><div style="font-weight:600; margin-bottom:4px;">${escapeHTML(pathLabel)}</div>${escapeHTML(method || "已打开文件")}</div>`
            );
        } else if (log.action === "browser_extract_text") {
            const message = log.message || log.result || {};
            const matched = message?.matched_text || message?.text || "(no match)";
            const term = message?.matched_term || log?.params?.text || "";
            const fullText = typeof message?.full_text === "string" ? message.full_text : "";
            const preview = fullText ? escapeHTML(fullText.slice(0, 300)) : "浏览器文本提取完成";
            const header = term ? `${escapeHTML(term)} → ${escapeHTML(matched)}` : escapeHTML(matched);
            appendAgentHTML(
                `<div class="bubble" style="background:#F0F7FF; color:#0F2744; white-space:pre-wrap;"><div style="font-weight:600; margin-bottom:4px;">${header}</div>${preview}</div>`
            );
        } else if (log.action === "list_files") {
            const message = log.message || log.result || {};
            const entries = message.entries || message.result?.entries;
            const pathLabel = message?.path || log?.params?.path || "directory";
            if (!Array.isArray(entries) || entries.length === 0) {
                appendAgentHTML(
                    `<div class="bubble" style="background:#F8F8F8; color:var(--text-main);"><div style="font-weight:600; margin-bottom:4px;">${escapeHTML(pathLabel)}</div>空目录或读取失败</div>`
                );
                return;
            }
            const lines = entries
                .map((e) => {
                    const isDir = e?.is_dir ? "[DIR]" : "     ";
                    const name = e?.name || "";
                    return `${isDir} ${name}`;
                })
                .slice(0, 100) // cap to avoid huge dumps
                .map(escapeHTML)
                .join("<br>");
            appendAgentHTML(
                `<div class="bubble" style="background:#F8F8F8; color:var(--text-main); white-space:pre-wrap;"><div style="font-weight:600; margin-bottom:4px;">${escapeHTML(pathLabel)}</div>${lines}</div>`
            );
        }
    });

    const errorLog = logs.find((l) => l.status === "error" || l.status === "unsafe");
    if (errorLog) {
        const msgObj = errorLog.message || errorLog.reason || errorLog;
        const text =
            (typeof msgObj === "string" && msgObj) ||
            msgObj?.reason ||
            msgObj?.message ||
            msgObj?.error ||
            JSON.stringify(msgObj);
        const actionLabel = errorLog.action ? ` (${errorLog.action})` : "";
        appendAgentHTML(
            `<div class="bubble" style="color:var(--error-color); background:#FFF5F5;">${escapeHTML(text)}${escapeHTML(actionLabel)}</div>`
        );
    }
}

// --- Core Logic ---

async function handleRun() {
    const text = (inputEl.value || "").trim();
    if (!text) return;
    stopEvidenceStream();
    clearEvidence();

    const api = getApi();
    const runFn = api.run;
    const chatMode = currentMode === "chat";
    const startedAt = performance.now();
    let durationLogged = false;
    const modeLabel = chatMode ? "对话" : "执行";
    const logDuration = () => {
        if (durationLogged) return;
        durationLogged = true;
        const seconds = ((performance.now() - startedAt) / 1000).toFixed(2);
        appendAgentHTML(
            `<div class="bubble" style="color:var(--text-sub); background:#F0F4FF;">⏱️ ${escapeHTML(
                modeLabel
            )}耗时 ${seconds}s</div>`
        );
    };
    if (!runFn || typeof runFn !== "function") {
        appendAgentHTML('<div class="bubble" style="color:var(--error-color); background:#FFF5F5;">Backend bridge unavailable. Please restart the app.</div>');
        setStatus("error");
        logDuration();
        return;
    }

    // 1. Prepare UI
    if (isSettingsOpen) toggleSettings(); // close settings if open
    toggleExpand(true); 
    inputEl.value = "";
    inputEl.blur();
    appendUserMessage(text);
    setStatus("busy");
    
    const loadingId = "loading-" + Date.now();
    appendAgentHTML(`<div id="${loadingId}" class="bubble" style="color:var(--text-sub); font-style:italic">Thinking...</div>`);

    try {
        // 2. Auto Capture Context (Silent capture unless it fails)
        if (!screenshotMeta) {
            await captureScreenshot(false); 
        }

        // 3. Call Backend with Work Dir
        let result;
        if (chatMode) {
            let timeoutHandle = null;
            try {
                const controller = new AbortController();
                timeoutHandle = setTimeout(() => controller.abort(), CHAT_TIMEOUT_MS);
                const payload = {
                    provider: currentProvider,
                    text,
                    screenshot_base64: screenshotBase64,
                };
                const res = await fetch(`${API_BASE}/api/ai/query`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                    signal: controller.signal,
                });
                clearTimeout(timeoutHandle);
                if (!res.ok) {
                    result = { error: `chat_call_failed: http ${res.status}` };
                } else {
                    result = await res.json();
                }
            } catch (err) { // AbortError or network failure
                result = { error: `chat_call_failed: ${err?.name === "AbortError" ? "request timed out" : err}` };
                log("ERROR", `Chat call failed: ${result.error}`);
            } finally {
                if (timeoutHandle) clearTimeout(timeoutHandle);
            }
        } else {
            result = await runFn(
                text,
                "", // ocrText
                null, // manualClick
                screenshotMeta,
                dryRunToggle?.checked ?? false,
                currentWorkDir || workDirInput.value, // Pass Work Directory
                screenshotBase64,
                currentProvider
            );
        }

        // 4. Update UI
        const loadingEl = document.getElementById(loadingId);
        if (loadingEl) loadingEl.remove();

        if (chatMode) {
            if (!result || result.error || result.status === "error") {
                appendAgentHTML(
                    `<div class="bubble" style="color:var(--error-color); background:#FFF5F5;">${result?.error || result?.message || "chat call failed"}</div>`
                );
                setStatus("error");
                logDuration();
                return;
            }
            const reply =
                (typeof result === "string" && result) ||
                result.response ||
                result.raw ||
                result.message ||
                (result.plan ? JSON.stringify(result.plan) : "") ||
                JSON.stringify(result);
            appendAgentHTML(`<div class="bubble">${reply}</div>`);
            setStatus("success");
            logDuration();
        } else {
            if (result && result.request_id) {
                startEvidenceStream(result.request_id);
            }
            // Show Plan
            const planEl = appendAgentHTML(createPlanCardWrapper(result));
            bindClarificationButtons(planEl);

            const summaryCard = createExecutionSummaryCard(result);
            if (summaryCard) {
                appendAgentHTML(summaryCard);
            }

            appendExecutionDetails(result);
            const evidenceCard = createEvidenceCard(result);
            if (evidenceCard) {
                appendAgentHTML(evidenceCard);
            }
            
            // Show Summary / Execution Status
            if (result.execution) {
                const logs = Array.isArray(result.execution.logs) ? result.execution.logs : [];
                const fails = logs.filter(l => l.status === 'error' || l.status === 'unsafe').length;
                if (fails > 0) setStatus("error");
                else setStatus("success");
            } else {
                // Dry run successful
                setStatus("success");
            }
            logDuration();
        }

    } catch (err) {
        const loadingEl = document.getElementById(loadingId);
        if (loadingEl) loadingEl.remove();
        appendAgentHTML(`<div class="bubble" style="color:var(--error-color); background:#FFF5F5;">Error: ${err}</div>`);
        setStatus("error");
        logDuration();
    }
}

// --- Vision Logic ---

async function captureScreenshot(showCard = true) {
    setStatus("busy");
    try {
        // Try raw capture first (faster)
        let res = await fetch(`${API_BASE}/api/screenshot/raw`, { method: "POST" });
        if (!res.ok) {
            // Fallback to vision screenshot
            res = await fetch(`${API_BASE}/api/vision/screenshot`, { method: "POST" });
        }
        if (!res.ok) {
            throw new Error(`screenshot request failed (${res.status})`);
        }

        const data = await res.json();
        const base64 = data.image_base64 || data.image;
        if (!base64) {
            throw new Error("screenshot missing image data");
        }

        screenshotBase64 = base64;
        screenshotMeta = {
            width: data.width,
            height: data.height
        };
        screenshotMain.onload = () => {
            if (!screenshotMeta.width || !screenshotMeta.height) {
                screenshotMeta = {
                    width: screenshotMain.naturalWidth,
                    height: screenshotMain.naturalHeight
                };
            }
        };
        screenshotMain.src = `data:image/png;base64,${base64}`; // Keep hidden img updated for legacy logic

        if (showCard) {
            toggleExpand(true);
            appendAgentHTML(createScreenshotCard(base64));
        }
        setStatus("idle");
    } catch (err) {
        setStatus("error");
        console.error("Screenshot failed:", err);
        log("ERROR", `Screenshot failed: ${err}`);
        if (showCard) {
            const msg = (err && err.message) ? err.message : "Screenshot failed";
            appendAgentHTML(`<div class="bubble" style="color:var(--error-color)">${msg}</div>`);
        }
    }
}

async function runOCR() {
    console.log("OCR triggered (hidden)");
}

// --- Event Listeners ---

runBtn.addEventListener("click", handleRun);
inputEl.addEventListener("keypress", (e) => {
    if (e.key === "Enter") handleRun();
});

expandBtn.addEventListener("click", () => {
    // If settings are open, close them and go back to chat
    if (isSettingsOpen) toggleSettings();
    else toggleExpand();
});

settingsBtn.addEventListener("click", toggleSettings);

modelButtons.forEach((btn) => {
    btn.addEventListener("click", () => applyProvider(btn.dataset.provider));
});

modeButtons.forEach((btn) => {
    btn.addEventListener("click", () => applyMode(btn.dataset.mode));
});

quitBtn.addEventListener("click", () => {
    if (!confirm("Are you sure you want to exit?")) return;
    const api = getApi();
    if (api && typeof api.exitApp === "function") {
        api.exitApp();
    } else {
        window.close();
    }
});

screenshotBtn.addEventListener("click", () => captureScreenshot(true));

// Hidden OCR button support
if (ocrBtn) ocrBtn.addEventListener("click", runOCR);

if (workDirChooseBtn) workDirChooseBtn.addEventListener("click", () => { void chooseWorkDir(); });
if (workDirInput) workDirInput.addEventListener("click", () => { void chooseWorkDir(); });

clearBtn.addEventListener("click", () => {
    chatScroll.innerHTML = '<div class="bubble">System ready.</div>';
    setStatus("idle");
});

if (evidenceClear) {
    evidenceClear.addEventListener("click", clearEvidence);
}
if (artifactClose) {
    artifactClose.addEventListener("click", closeArtifactModal);
}
if (artifactModal) {
    artifactModal.addEventListener("click", (e) => {
        if (e.target === artifactModal) {
            closeArtifactModal();
        }
    });
}

setStatus(currentStatusState);
startHealthMonitor();
