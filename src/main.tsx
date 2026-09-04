import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { healthCheck, setSidecarBase } from "./lib/api";
import "./index.css";

function Bootstrap() {
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const start = Date.now();

    async function waitForSidecar() {
      const base = await window.electronAPI?.getSidecarUrl().catch(() => "http://127.0.0.1:18765");
      setSidecarBase(base || "http://127.0.0.1:18765");

      while (!cancelled && Date.now() - start < 60000) {
        try {
          await healthCheck();
          if (!cancelled) setReady(true);
          return;
        } catch {
          await new Promise((r) => setTimeout(r, 500));
        }
      }
      if (!cancelled) {
        setError(
          "OCR 服务启动超时。请确认已安装 Anaconda 的 OCR 环境，或设置环境变量 OCR_PYTHON 指向 python.exe 后重启。"
        );
      }
    }

    waitForSidecar();
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3 bg-canvas px-4 text-center">
        <p className="text-sm text-crimson-700">{error}</p>
        <button
          type="button"
          className="rounded-lg bg-crimson-700 px-4 py-2 text-sm text-white"
          onClick={() => window.location.reload()}
        >
          重试
        </button>
      </div>
    );
  }

  if (!ready) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-2 bg-canvas">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-crimson-700 border-t-transparent" />
        <p className="text-sm text-muted">正在启动 OCR 服务…</p>
      </div>
    );
  }

  return <App />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Bootstrap />
    </BrowserRouter>
  </StrictMode>
);
