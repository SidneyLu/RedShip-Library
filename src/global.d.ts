export interface ElectronAPI {
  openPdf: () => Promise<string[] | null>;
  openFolder: () => Promise<string | null>;
  getSidecarUrl: () => Promise<string>;
  getVersion: () => Promise<string>;
  openPath: (path: string) => Promise<void>;
}

declare global {
  interface Window {
    electronAPI?: ElectronAPI;
  }
}

export {};
