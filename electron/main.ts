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

function getSidecarCommand(): { cmd: string; args: string[]; cwd?: string } {
  const isDev = !app.isPackaged;
  if (isDev) {
    const sidecarDir = path.join(__dirname, "..", "sidecar");
    const py = resolveDevPython();
    if (py) {
      return {
        cmd: py,
        args: ["main.py", "--port", String(SIDECAR_PORT)],
        cwd: sidecarDir,
      };
    }
    // Prefer bundled sidecar exe over Windows Store "python" stub
    const localExe = path.join(__dirname, "..", "resources", "ocr-sidecar", "ocr-sidecar.exe");
    if (process.platform === "win32" && fs.existsSync(localExe)) {
      return { cmd: localExe, args: ["--port", String(SIDECAR_PORT)] };
    }
    return {
      cmd: process.platform === "win32" ? "python" : "python3",
      args: ["main.py", "--port", String(SIDECAR_PORT)],
      cwd: sidecarDir,
    };
  }
  const exe = path.join(process.resourcesPath, "ocr-sidecar", "ocr-sidecar.exe");
  return {
    cmd: exe,
    args: ["--port", String(SIDECAR_PORT)],
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

function startSidecar(): void {
  const { cmd, args, cwd } = getSidecarCommand();
  console.log("[sidecar] starting", cmd, args.join(" "));
  sidecarProcess = spawn(cmd, args, {
    cwd,
    env: { ...process.env },
    stdio: "pipe",
  });
  sidecarProcess.stdout?.on("data", (d) => console.log("[sidecar]", d.toString()));
  sidecarProcess.stderr?.on("data", (d) => {
    const text = d.toString();
    // WinError 10048 / EADDRINUSE: another sidecar already owns the port — safe to ignore
    if (/10048|address already in use|EADDRINUSE/i.test(text)) {
      console.log("[sidecar] port already in use; using existing process");
      return;
    }
    console.error("[sidecar]", text);
  });
  sidecarProcess.on("error", (err) => {
    console.error("[sidecar] failed to start:", err.message);
  });
  sidecarProcess.on("exit", (code) => {
    console.log("Sidecar exited", code);
    sidecarProcess = null;
  });
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
  if ((await isSidecarUp()) || (await isPortInUse(SIDECAR_PORT))) {
    console.log("[sidecar] already running, skip spawn");
  } else {
    startSidecar();
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

ipcMain.handle("app:getVersion", () => app.getVersion());

ipcMain.handle("shell:openPath", async (_e, p: string) => {
  if (p) await shell.openPath(p);
});
