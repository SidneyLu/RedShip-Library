import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("electronAPI", {
  openPdf: () => ipcRenderer.invoke("dialog:openPdf") as Promise<string[] | null>,
  openFolder: () => ipcRenderer.invoke("dialog:openFolder") as Promise<string | null>,
  getSidecarUrl: () => ipcRenderer.invoke("app:getSidecarUrl") as Promise<string>,
  getSidecarStatus: () =>
    ipcRenderer.invoke("app:getSidecarStatus") as Promise<{
      packaged: boolean;
      url: string;
      running: boolean;
      command: string | null;
      error: string | null;
    }>,
  getVersion: () => ipcRenderer.invoke("app:getVersion") as Promise<string>,
  openPath: (p: string) => ipcRenderer.invoke("shell:openPath", p) as Promise<void>,
});
