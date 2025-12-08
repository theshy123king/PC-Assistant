const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");
const { app, BrowserWindow, dialog, ipcMain } = require("electron");

const PROJECT_ROOT = path.join(__dirname, "..");
const BACKEND_CWD = PROJECT_ROOT; 
const VENV_PYTHON_WIN = path.join(__dirname, "..", "venv", "Scripts", "python.exe");
let backendProcess = null;

function resolvePython() {
  if (process.env.PYTHON && process.env.PYTHON.trim()) {
    return process.env.PYTHON.trim();
  }
  if (fs.existsSync(VENV_PYTHON_WIN)) {
    return VENV_PYTHON_WIN;
  }
  return "python"; 
}

function startBackend() {
  if (backendProcess) return;

  const pythonCmd = resolvePython();
  const args = [
    "-m",
    "uvicorn",
    "backend.app:app",
    "--host",
    "127.0.0.1",
    "--port",
    "8000",
  ];

  try {
    backendProcess = spawn(pythonCmd, args, {
      cwd: BACKEND_CWD,
      env: { ...process.env },
      stdio: "inherit",
      shell: true,
    });
  } catch (err) {
    dialog.showErrorBox("Backend launch failed", String(err));
    backendProcess = null;
    return;
  }

  backendProcess.on("exit", (code, signal) => {
    console.log(`Backend exited with code ${code} signal ${signal}`);
    backendProcess = null;
  });

  backendProcess.on("error", (err) => {
    console.error("Failed to start backend:", err);
    backendProcess = null;
  });
}

function stopBackend() {
  if (!backendProcess) return;
  
  if (process.platform === "win32" && backendProcess.pid) {
    try {
      spawn("taskkill", ["/pid", backendProcess.pid, "/T", "/F"]);
    } catch (err) {
      console.error("taskkill failed", err);
      backendProcess.kill();
    }
  } else {
    backendProcess.kill();
  }
  backendProcess = null;
}

function shutdownApp() {
  try {
    stopBackend();
  } catch (err) {
    console.error("stopBackend failed", err);
  }
  try {
    const windows = BrowserWindow.getAllWindows();
    windows.forEach((w) => {
      try {
        w.destroy();
      } catch (e) {
        console.error("Failed to destroy window", e);
      }
    });
  } catch (err) {
    console.error("destroy windows failed", err);
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
    stopBackend();
    app.quit();
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
  shutdownApp();
});

app.whenReady().then(() => {
  startBackend();
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  stopBackend();
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", () => {
  stopBackend();
});

process.on("exit", () => {
  stopBackend();
});
