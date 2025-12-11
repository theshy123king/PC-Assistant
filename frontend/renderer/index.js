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
const PROVIDER_LABELS = { deepseek: "DeepSeek", doubao: "Doubao", qwen: "Qwen" };
const PROVIDER_STORAGE_KEY = "pc_assistant_provider";
const modeOptions = document.getElementById("mode-options");
const modeButtons = modeOptions ? Array.from(modeOptions.querySelectorAll(".pill-btn")) : [];
const MODE_STORAGE_KEY = "pc_assistant_mode";
const MODE_LABELS = { execute: "Execute", chat: "Chat" };
const DEFAULT_API_BASE = "http://127.0.0.1:5004";
const API_BASE = (window.api && window.api.backendBaseUrl) || DEFAULT_API_BASE;
const CHAT_TIMEOUT_MS = 45000;
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
    
    const planObj = data.plan || data.plan_after_injection || data;
    const steps = planObj.steps || [];
    
    if (steps.length === 0) {
        return `<div class="bubble">No steps generated.</div>`;
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

function appendExecutionDetails(result) {
    const execution = result?.execution || result;
    const logs = Array.isArray(execution?.logs) ? execution.logs : [];
    if (!logs.length) return;

    logs.forEach((log) => {
        if (log.status !== "success") return;
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

    const api = getApi();
    const runFn = api.run;
    const chatMode = currentMode === "chat";
    if (!runFn || typeof runFn !== "function") {
        appendAgentHTML('<div class="bubble" style="color:var(--error-color); background:#FFF5F5;">Backend bridge unavailable. Please restart the app.</div>');
        setStatus("error");
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
        } else {
            // Show Plan
            appendAgentHTML(createPlanCardWrapper(result));

            const summaryCard = createExecutionSummaryCard(result);
            if (summaryCard) {
                appendAgentHTML(summaryCard);
            }

            appendExecutionDetails(result);
            
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
        }

    } catch (err) {
        const loadingEl = document.getElementById(loadingId);
        if (loadingEl) loadingEl.remove();
        appendAgentHTML(`<div class="bubble" style="color:var(--error-color); background:#FFF5F5;">Error: ${err}</div>`);
        setStatus("error");
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

setStatus(currentStatusState);
startHealthMonitor();
