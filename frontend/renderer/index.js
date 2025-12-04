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
const statusDot = document.getElementById("statusDot");
const statusText = document.getElementById("clickPickerStatus");
const panelTitle = document.getElementById("panel-title");
const modelOptions = document.getElementById("model-options");
const modelButtons = modelOptions ? Array.from(modelOptions.querySelectorAll(".pill-btn")) : [];
const PROVIDER_LABELS = { deepseek: "DeepSeek", openai: "OpenAI", qwen: "Qwen" };
const PROVIDER_STORAGE_KEY = "pc_assistant_provider";

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
let currentStatusState = "idle";

function getApi() {
    if (window.api) return window.api;
    // Fallback: minimal bridge to keep UI usable if preload failed.
    return {
        defaultWorkDir: "",
        run: async (text, ocrText = "", manualClick = null, screenshotMeta = null, dryRun = false, workDir = null, screenshotBase64 = null, provider = currentProvider) => {
            try {
                const res = await fetch("http://127.0.0.1:8000/api/ai/run", {
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

function syncProviderButtons(provider) {
    if (!modelButtons || modelButtons.length === 0) return;
    const normalized = normalizeProvider(provider);
    modelButtons.forEach((btn) => {
        const btnProvider = normalizeProvider(btn.dataset.provider);
        if (btnProvider === normalized) btn.classList.add("active");
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

// Initialize Settings
(function initWorkDir() {
    const api = getApi();
    if (api && api.defaultWorkDir) {
        workDirInput.value = api.defaultWorkDir;
    }
})();

(function initProviderSelection() {
    const stored = loadStoredProvider();
    if (stored && PROVIDER_LABELS[stored]) {
        currentProvider = stored;
    }
    syncProviderButtons(currentProvider);
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
    return `${text} | ${getProviderLabel()}`;
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
}

function scrollToBottom() {
    chatScroll.scrollTop = chatScroll.scrollHeight;
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

// --- Core Logic ---

async function handleRun() {
    const text = (inputEl.value || "").trim();
    if (!text) return;

    const api = getApi();
    const runFn = api.run;
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
        const result = await runFn(
            text,
            "", // ocrText
            null, // manualClick
            screenshotMeta,
            dryRunToggle?.checked ?? false,
            workDirInput.value, // Pass Work Directory
            screenshotBase64,
            currentProvider
        );

        // 4. Update UI
        const loadingEl = document.getElementById(loadingId);
        if (loadingEl) loadingEl.remove();

        if (result.error) {
            appendAgentHTML(`<div class="bubble" style="color:var(--error-color); background:#FFF5F5;">${result.error}</div>`);
            setStatus("error");
            return;
        }
        
        // Show Plan
        appendAgentHTML(createPlanCardWrapper(result));

        const summaryCard = createExecutionSummaryCard(result);
        if (summaryCard) {
            appendAgentHTML(summaryCard);
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

    } catch (err) {
        const loadingEl = document.getElementById(loadingId);
        if (loadingEl) loadingEl.remove();
        appendAgentHTML(`<div class="bubble" style="color:var(--error-color); background:#FFF5F5;">Error: ${err}</div>`);
        setStatus("error");
    }
}

// --- Vision Logic ---

async function captureScreenshot(showCard = true) {
    try {
        setStatus("busy");
        // Try raw capture first (faster)
        let res = await fetch("http://127.0.0.1:8000/api/screenshot/raw", { method: "POST" });
        if (!res.ok) {
            // Fallback to vision screenshot
            res = await fetch("http://127.0.0.1:8000/api/vision/screenshot", { method: "POST" });
        }
        
        const data = await res.json();
        const base64 = data.image_base64 || data.image;
        
        if (base64) {
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
        }
    } catch (err) {
        setStatus("error");
        console.error("Screenshot failed:", err);
        if (showCard) {
            appendAgentHTML(`<div class="bubble" style="color:var(--error-color)">Screenshot failed</div>`);
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

clearBtn.addEventListener("click", () => {
    chatScroll.innerHTML = '<div class="bubble">System ready.</div>';
    setStatus("idle");
});

setStatus(currentStatusState);
