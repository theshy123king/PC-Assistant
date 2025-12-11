const fs = require("fs");
const http = require("http");
const path = require("path");
const { spawn } = require("child_process");
const { app, BrowserWindow, dialog, ipcMain } = require("electron");

const PROJECT_ROOT = path.join(__dirname, "..");
const BACKEND_CWD = PROJECT_ROOT;
const BACKEND_HOST = process.env.PC_ASSISTANT_DEV_HOST || "127.0.0.1";
const BACKEND_PORT = Number(process.env.PC_ASSISTANT_DEV_PORT || "5004");
const VENV_PYTHON_WIN = path.join(__dirname, "..", "venv", "Scripts", "python.exe");
const LOG_DIR = path.join(PROJECT_ROOT, "logs");
const LOG_MAIN = path.join(LOG_DIR, "electron-main.log");
const LOG_RENDERER = path.join(LOG_DIR, "electron-renderer.log");
const MAX_LOG_SIZE = 1_000_000; // ~1MB
let backendProcess = null;
let backendStarting = false;
let isQuitting = false;

function ensureLogDir() {
  try {
    if (!fs.existsSync(LOG_DIR)) {
      fs.mkdirSync(LOG_DIR, { recursive: true });
    }
  } catch (err) {
    console.error("Failed to ensure log dir:", err);
  }
}

function rotateIfNeeded(filePath) {
  try {
    const stats = fs.statSync(filePath);
    if (stats.size < MAX_LOG_SIZE) return;
    const backup = `${filePath}.1`;
    try {
      fs.unlinkSync(backup);
    } catch (err) {
      /* ignore */
    }
    fs.renameSync(filePath, backup);
  } catch (err) {
    // ignore missing file or rotation errors
  }
}

function appendLog(filePath, level, message) {
  ensureLogDir();
  const ts = new Date().toISOString();
  const line = `[${ts}] [${level}] ${message}\n`;
  try {
    rotateIfNeeded(filePath);
    fs.appendFile(filePath, line, { encoding: "utf-8" }, (err) => {
      if (err) {
        console.error("log append failed", err);
      }
    });
  } catch (err) {
    console.error("log write failed", err);
  }
}

function logMain(level, message) {
  appendLog(LOG_MAIN, level, message);
}

function logRenderer(level, message) {
  appendLog(LOG_RENDERER, level, message);
}

ensureLogDir();
logMain("INFO", "Electron main starting");

function isBackendHealthy() {
  return new Promise((resolve) => {
    const req = http.get(
      { host: BACKEND_HOST, port: BACKEND_PORT, path: "/", timeout: 1000 },
      (res) => {
        let body = "";
        res.on("data", (c) => {
          body += c.toString();
        });
        res.on("end", () => {
          resolve(res.statusCode === 200 && body.includes("backend running"));
        });
      }
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
  });
}

function findPidsByPort(port) {
  try {
    const { execSync } = require("child_process");
    const result = execSync(`netstat -ano -p tcp | findstr :${port}`, { encoding: "utf-8" });
    const pids = new Set();
    result
      .split(/\r?\n/)
      .map((l) => l.trim())
      .filter(Boolean)
      .forEach((line) => {
        const parts = line.split(/\s+/);
        const pid = parts[parts.length - 1];
        if (pid && /^\d+$/.test(pid)) {
          pids.add(Number(pid));
        }
      });
    return Array.from(pids);
  } catch (err) {
    logMain("ERROR", `findPidsByPort failed: ${err}`);
    return [];
  }
}

function resolvePython() {
  if (process.env.PYTHON && process.env.PYTHON.trim()) {
    logMain("INFO", "Using PYTHON env override for backend");
    return process.env.PYTHON.trim();
  }
  if (fs.existsSync(VENV_PYTHON_WIN)) {
    logMain("INFO", "Using venv python for backend");
    return VENV_PYTHON_WIN;
  }
  logMain("INFO", "Falling back to system python for backend");
  return "python"; 
}

async function startBackend() {
  if (backendProcess || backendStarting) return;
  backendStarting = true;
  logMain("INFO", "Starting backend via launcher");

  const pythonCmd = resolvePython();
  const args = [
    "-m",
    "backend.launch_backend",
    "--host",
    BACKEND_HOST,
    "--port",
    String(BACKEND_PORT),
  ];

  try {
    backendProcess = spawn(pythonCmd, args, {
      cwd: BACKEND_CWD,
      env: { ...process.env },
      stdio: ["ignore", "pipe", "pipe"],
      shell: true,
    });
    logMain("INFO", `Spawned backend process pid=${backendProcess.pid}`);
    if (backendProcess.stdout) {
      backendProcess.stdout.on("data", (chunk) => {
        const text = String(chunk || "").trimEnd();
        if (text) logMain("INFO", `[backend stdout] ${text}`);
      });
    }
    if (backendProcess.stderr) {
      backendProcess.stderr.on("data", (chunk) => {
        const text = String(chunk || "").trimEnd();
        if (text) logMain("ERROR", `[backend stderr] ${text}`);
      });
    }
  } catch (err) {
    dialog.showErrorBox("Backend launch failed", String(err));
    logMain("ERROR", `Backend launch failed: ${err}`);
    backendProcess = null;
    backendStarting = false;
    return;
  }

  backendProcess.on("exit", (code, signal) => {
    console.log(`Backend exited with code ${code} signal ${signal}`);
    logMain("WARNING", `Backend exited code=${code} signal=${signal}`);
    backendProcess = null;
    backendStarting = false;
    // Treat normal app shutdown as clean: if app is quitting, skip dialog.
    if (isQuitting || app.isQuitting || code === 0 || code === null || code === undefined) {
      return;
    }
    if (code && code !== 0) {
      // Check if backend is actually up (another instance or already running)
      isBackendHealthy()
        .then((healthy) => {
          if (healthy) {
            logMain("INFO", "Backend already running; suppressing exit dialog.");
            return;
          }
          dialog.showErrorBox(
            "Backend exited",
            `Backend process ended unexpectedly (code ${code}${signal ? `, signal ${signal}` : ""}).`
          );
        })
        .catch(() => {
          dialog.showErrorBox(
            "Backend exited",
            `Backend process ended unexpectedly (code ${code}${signal ? `, signal ${signal}` : ""}).`
          );
        });
    }
  });

  backendProcess.on("error", (err) => {
    console.error("Failed to start backend:", err);
    logMain("ERROR", `Backend process error: ${err}`);
    backendProcess = null;
    backendStarting = false;
  });
  backendStarting = false;
}

function stopBackend() {
  if (!backendProcess) {
    if (process.platform === "win32") {
      const pids = findPidsByPort(BACKEND_PORT);
      if (pids.length) {
        logMain("INFO", `Stopping backend by port; pids=${pids.join(",")}`);
        pids.forEach((pid) => {
          try {
            spawn("taskkill", ["/pid", String(pid), "/T", "/F"]);
          } catch (err) {
            logMain("ERROR", `taskkill by port failed pid=${pid}: ${err}`);
          }
        });
      }
    }
    return;
  }
  
  if (process.platform === "win32" && backendProcess.pid) {
    try {
      logMain("INFO", `Stopping backend via taskkill pid=${backendProcess.pid}`);
      spawn("taskkill", ["/pid", backendProcess.pid, "/T", "/F"]);
    } catch (err) {
      console.error("taskkill failed", err);
      logMain("ERROR", `taskkill failed: ${err}`);
      backendProcess.kill();
    }
  } else {
    logMain("INFO", `Stopping backend pid=${backendProcess.pid}`);
    backendProcess.kill();
  }
  backendProcess = null;
}

function shutdownApp({ forceExit = false } = {}) {
  isQuitting = true;
  app.isQuitting = true;
  try {
    stopBackend();
  } catch (err) {
    console.error("stopBackend failed", err);
    logMain("ERROR", `stopBackend failed: ${err}`);
  }
  try {
    const windows = BrowserWindow.getAllWindows();
    windows.forEach((w) => {
      try {
        w.destroy();
      } catch (e) {
        console.error("Failed to destroy window", e);
        logMain("ERROR", `Window destroy failed: ${e}`);
      }
    });
  } catch (err) {
    console.error("destroy windows failed", err);
    logMain("ERROR", `Destroy windows failed: ${err}`);
  }
  if (forceExit && process.platform === "win32") {
    logMain("INFO", "Force exiting app (Windows)");
    app.exit(0);
    return;
  }
  app.quit();
}

function createWindow() {
  const win = new BrowserWindow({
    width: 420,        // 挂件宽度
    height: 600,       // 挂件高度
    transparent: true, // 关键：透明背景
    frame: false,      // 关键：无边框
    hasShadow: false,  // 让 CSS 处理阴影
    resizable: false,  // 固定大小
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  win.loadFile(path.join(__dirname, "renderer", "index.html"));

  // If window gets closed via fallback window.close(), still stop backend and quit.
  win.on("closed", () => {
    shutdownApp({ forceExit: process.platform === "win32" });
  });
}

ipcMain.handle("select-work-dir", async () => {
  const result = await dialog.showOpenDialog(BrowserWindow.getFocusedWindow() || undefined, {
    properties: ["openDirectory"],
    defaultPath: path.join(app.getPath("home"), "Desktop"),
  });
  if (result.canceled || !result.filePaths || result.filePaths.length === 0) {
    return null;
  }
  return result.filePaths[0];
});

// Listen for renderer exit requests (quit button)
ipcMain.on("exit-app", () => {
  shutdownApp({ forceExit: process.platform === "win32" });
});

app.whenReady().then(() => {
  void startBackend();
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform === "darwin") {
    stopBackend();
    return;
  }
  shutdownApp({ forceExit: process.platform === "win32" });
});

app.on("before-quit", () => {
  isQuitting = true;
  app.isQuitting = true;
  stopBackend();
});

process.on("exit", () => {
  stopBackend();
});

ipcMain.handle("restart-backend", async () => {
  try {
    logMain("WARNING", "Renderer requested backend restart");
    stopBackend();
    await startBackend();
    return { ok: true };
  } catch (err) {
    logMain("ERROR", `Restart backend failed: ${err}`);
    return { ok: false, error: String(err) };
  }
});

ipcMain.handle("renderer-log", async (_event, payload) => {
  try {
    const level = (payload?.level || "INFO").toUpperCase();
    const message = String(payload?.message || "");
    logRenderer(level, message);
    return { ok: true };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
});
