import { app, BrowserWindow, dialog, ipcMain, shell } from "electron";
import { spawn, ChildProcess } from "child_process";
import fs from "fs";
import net from "net";
import os from "os";
import path from "path";

let mainWindow: BrowserWindow | null = null;
let sidecarProcess: ChildProcess | null = null;
const SIDECAR_PORT = 18765;
const SIDECAR_URL = `http://127.0.0.1:${SIDECAR_PORT}`;

type SidecarStatus = {
  packaged: boolean;
  url: string;
  running: boolean;
  command: string | null;
  error: string | null;
};

let lastSidecarError: string | null = null;
let lastSidecarCommand: string | null = null;

function resolveDevPython(): string | null {
  const candidates: string[] = [];
  if (process.env.OCR_PYTHON) candidates.push(process.env.OCR_PYTHON);
  if (process.env.CONDA_PREFIX) {
    candidates.push(
      path.join(process.env.CONDA_PREFIX, process.platform === "win32" ? "python.exe" : "bin/python")
    );
  }
  const home = os.homedir();
  if (process.platform === "win32") {
    candidates.push(
      path.join(home, "anaconda3", "envs", "OCR", "python.exe"),
      path.join(home, "miniconda3", "envs", "OCR", "python.exe"),
      path.join(home, "mambaforge", "envs", "OCR", "python.exe"),
      path.join(home, "AppData", "Local", "anaconda3", "envs", "OCR", "python.exe"),
      path.join(home, "AppData", "Local", "miniconda3", "envs", "OCR", "python.exe")
    );
  } else {
    candidates.push(
      path.join(home, "anaconda3", "envs", "OCR", "bin", "python"),
      path.join(home, "miniconda3", "envs", "OCR", "bin", "python")
    );
  }
  for (const p of candidates) {
    if (p && fs.existsSync(p)) return p;
  }
  return null;
}

function getBundledSidecarExe(): string {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "ocr-sidecar", "ocr-sidecar.exe");
  }
  return path.join(__dirname, "..", "resources", "ocr-sidecar", "ocr-sidecar.exe");
}

function getSidecarCommand(): { cmd: string; args: string[]; cwd?: string } {
  // Packaged install must use the bundled exe — never require Anaconda/Python.
  if (app.isPackaged) {
    const exe = getBundledSidecarExe();
    return { cmd: exe, args: ["--port", String(SIDECAR_PORT)] };
  }

  const sidecarDir = path.join(__dirname, "..", "sidecar");
  const bundled = getBundledSidecarExe();
  // Dev: prefer local bundled exe when present (matches production), else conda python.
  if (process.platform === "win32" && fs.existsSync(bundled)) {
    return { cmd: bundled, args: ["--port", String(SIDECAR_PORT)] };
  }
  const py = resolveDevPython();
  if (py) {
    return {
      cmd: py,
      args: ["main.py", "--port", String(SIDECAR_PORT)],
      cwd: sidecarDir,
    };
  }
  return {
    cmd: process.platform === "win32" ? "python" : "python3",
    args: ["main.py", "--port", String(SIDECAR_PORT)],
    cwd: sidecarDir,
  };
}

async function isSidecarUp(): Promise<boolean> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 4000);
  try {
    const res = await fetch(`${SIDECAR_URL}/health`, { signal: ctrl.signal });
    return res.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

/** True if something already accepts TCP on the sidecar port (even if /health is slow). */
function isPortInUse(port: number, host = "127.0.0.1"): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = net.connect({ port, host }, () => {
      socket.end();
      resolve(true);
    });
    socket.on("error", () => resolve(false));
    socket.setTimeout(800, () => {
      socket.destroy();
      resolve(false);
    });
  });
}

function startSidecar(): boolean {
  lastSidecarError = null;
  const { cmd, args, cwd } = getSidecarCommand();
  lastSidecarCommand = [cmd, ...args].join(" ");

  if (app.isPackaged || cmd.toLowerCase().endsWith("ocr-sidecar.exe")) {
    if (!fs.existsSync(cmd)) {
      lastSidecarError = `未找到内置 OCR 引擎：\n${cmd}\n请重新安装应用。`;
      console.error("[sidecar]", lastSidecarError);
      return false;
    }
  }

  console.log("[sidecar] starting", cmd, args.join(" "));
  try {
    sidecarProcess = spawn(cmd, args, {
      cwd,
      env: { ...process.env },
      stdio: "pipe",
      windowsHide: true,
    });
  } catch (err) {
    lastSidecarError = `启动 OCR 引擎失败：${(err as Error).message}`;
    console.error("[sidecar]", lastSidecarError);
    return false;
  }

  sidecarProcess.stdout?.on("data", (d) => console.log("[sidecar]", d.toString()));
  sidecarProcess.stderr?.on("data", (d) => {
    const text = d.toString();
    if (/10048|address already in use|EADDRINUSE/i.test(text)) {
      console.log("[sidecar] port already in use; using existing process");
      return;
    }
    console.error("[sidecar]", text);
    // Keep a short tail for UI diagnostics
    const line = text.trim().split(/\r?\n/).filter(Boolean).pop();
    if (line) lastSidecarError = line.slice(0, 500);
  });
  sidecarProcess.on("error", (err) => {
    lastSidecarError = `OCR 引擎进程错误：${err.message}`;
    console.error("[sidecar] failed to start:", err.message);
  });
  sidecarProcess.on("exit", (code) => {
    console.log("Sidecar exited", code);
    if (code && code !== 0 && !lastSidecarError) {
      lastSidecarError = `OCR 引擎异常退出（code=${code}）`;
    }
    sidecarProcess = null;
  });
  return true;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  if (app.isPackaged) {
    mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  } else {
    mainWindow.loadURL("http://localhost:5173");
    mainWindow.webContents.openDevTools({ mode: "detach" });
  }
}

app.whenReady().then(async () => {
  createWindow();
  // Only skip spawn when /health is actually OK. A stale process holding the
  // port (without serving HTTP) would otherwise leave the UI waiting forever.
  if (await isSidecarUp()) {
    console.log("[sidecar] already healthy, skip spawn");
  } else {
    if (await isPortInUse(SIDECAR_PORT)) {
      console.warn(
        "[sidecar] port in use but /health failed; attempting spawn anyway (may fail with EADDRINUSE)"
      );
    }
    const ok = startSidecar();
    if (!ok && app.isPackaged && mainWindow) {
      dialog.showErrorBox("OCR Library", lastSidecarError || "无法启动内置 OCR 引擎");
    }
  }
});

app.on("window-all-closed", () => {
  if (sidecarProcess) {
    sidecarProcess.kill();
    sidecarProcess = null;
  }
  if (process.platform !== "darwin") app.quit();
});

ipcMain.handle("dialog:openPdf", async () => {
  const result = await dialog.showOpenDialog(mainWindow!, {
    properties: ["openFile", "multiSelections"],
    filters: [{ name: "PDF", extensions: ["pdf"] }],
  });
  return result.canceled || result.filePaths.length === 0 ? null : result.filePaths;
});

ipcMain.handle("dialog:openFolder", async () => {
  const result = await dialog.showOpenDialog(mainWindow!, {
    properties: ["openDirectory"],
  });
  return result.canceled || !result.filePaths[0] ? null : result.filePaths[0];
});

ipcMain.handle("app:getSidecarUrl", () => SIDECAR_URL);

ipcMain.handle("app:getSidecarStatus", async (): Promise<SidecarStatus> => {
  const running = (await isSidecarUp()) || (await isPortInUse(SIDECAR_PORT));
  return {
    packaged: app.isPackaged,
    url: SIDECAR_URL,
    running,
    command: lastSidecarCommand,
    error: lastSidecarError,
  };
});

ipcMain.handle("app:getVersion", () => app.getVersion());

ipcMain.handle("shell:openPath", async (_e, p: string) => {
  if (p) await shell.openPath(p);
});
