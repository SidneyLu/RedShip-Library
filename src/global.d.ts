export interface SidecarStatus {
  packaged: boolean;
  url: string;
  running: boolean;
  command: string | null;
  error: string | null;
}

export interface ElectronAPI {
  openPdf: () => Promise<string[] | null>;
  openFolder: () => Promise<string | null>;
  getSidecarUrl: () => Promise<string>;
  getSidecarStatus: () => Promise<SidecarStatus>;
  getVersion: () => Promise<string>;
  openPath: (path: string) => Promise<void>;
}

declare global {
  interface Window {
    electronAPI?: ElectronAPI;
  }
}

export {};
