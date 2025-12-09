const { contextBridge, ipcRenderer } = require("electron");
const path = require("path");
const os = require("os");

const BACKEND_HOST = process.env.PC_ASSISTANT_DEV_HOST || "127.0.0.1";
const BACKEND_PORT = Number(process.env.PC_ASSISTANT_DEV_PORT || "5004");
const API_BASE = `http://${BACKEND_HOST}:${BACKEND_PORT}`;
const PRELOAD_LOG_PREFIX = "[preload]";

async function log(level, message) {
  try {
    await ipcRenderer.invoke("renderer-log", { level, message: `${PRELOAD_LOG_PREFIX} ${message}` });
  } catch (err) {
    // ignore logging failures
  }
}

(function exposeApi() {
  try {
    const defaultWorkDir = path.join(os.homedir(), "Desktop");

    async function run(
      text,
      ocrText = "",
      manualClick = null,
      screenshotMeta = null,
      dryRun = false,
      workDir = null,
      screenshotBase64 = null,
      provider = "deepseek"
    ) {
      if (!text || typeof text !== "string") {
        return { error: "user_text is required" };
      }

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
            work_dir: workDir || defaultWorkDir,
            screenshot_base64: screenshotBase64,
            provider: provider || "deepseek",
          }),
        });
        const data = await res.json();
        return data;
      } catch (err) {
        log("ERROR", `run call failed: ${err}`);
        return { error: String(err) };
      }
    }

    async function selectWorkDir() {
      try {
        const selected = await ipcRenderer.invoke("select-work-dir");
        return selected || null;
      } catch (err) {
        log("ERROR", `selectWorkDir failed: ${err}`);
        return null;
      }
    }

    contextBridge.exposeInMainWorld("api", {
      run,
      exitApp: () => ipcRenderer.send("exit-app"),
      defaultWorkDir,
      selectWorkDir,
      backendBaseUrl: API_BASE,
      restartBackend: async () => {
        try {
          const result = await ipcRenderer.invoke("restart-backend");
          return result || { ok: true };
        } catch (err) {
          log("ERROR", `restartBackend failed: ${err}`);
          return { ok: false, error: String(err) };
        }
      },
      log: (level, message) => log(level, message),
    });
  } catch (err) {
    // Fallback exposure to avoid undefined bridge
    contextBridge.exposeInMainWorld("api", {
      run: async () => ({ error: "bridge_init_failed: " + err }),
      exitApp: () => ipcRenderer.send("exit-app"),
      defaultWorkDir: null,
      selectWorkDir: async () => null,
      backendBaseUrl: API_BASE,
      restartBackend: async () => ({ ok: false, error: "bridge_init_failed" }),
      log: async () => {},
    });
  }
})();
