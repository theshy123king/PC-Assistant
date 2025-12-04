const { contextBridge, ipcRenderer } = require("electron");
const path = require("path");
const os = require("os");

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
        const res = await fetch("http://127.0.0.1:8000/api/ai/run", {
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
        return { error: String(err) };
      }
    }

    contextBridge.exposeInMainWorld("api", {
      run,
      exitApp: () => ipcRenderer.send("exit-app"),
      defaultWorkDir,
    });
  } catch (err) {
    // Fallback exposure to avoid undefined bridge
    contextBridge.exposeInMainWorld("api", {
      run: async () => ({ error: "bridge_init_failed: " + err }),
      exitApp: () => ipcRenderer.send("exit-app"),
      defaultWorkDir: null,
    });
  }
})();
