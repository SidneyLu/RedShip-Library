import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { HashRouter } from "react-router-dom";
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

      // Packaged sidecar (PyInstaller) can take >60s on first cold start.
      const timeoutMs = 120_000;

      while (!cancelled && Date.now() - start < timeoutMs) {
        try {
          await healthCheck();
          if (!cancelled) setReady(true);
          return;
        } catch {
          await new Promise((r) => setTimeout(r, 500));
        }
      }

      if (cancelled) return;

      let detail = "";
      try {
        const st = await window.electronAPI?.getSidecarStatus?.();
        if (st?.error) detail = `\n\n详情：${st.error}`;
        if (st?.packaged) {
          setError(
            "内置 OCR 服务启动超时。安装版无需 Anaconda。" +
              "请确认杀毒软件未拦截 ocr-sidecar.exe，或重启应用重试。" +
              detail
          );
          return;
        }
      } catch {
        /* ignore */
      }

      setError(
        "OCR 服务启动超时。" +
          "开发模式请确认已安装 Python/OCR 依赖，或设置 OCR_PYTHON 后重启；" +
          "安装版应使用内置引擎，请重新安装应用。" +
          detail
      );
    }

    waitForSidecar();
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3 bg-canvas px-4 text-center">
        <p className="max-w-lg whitespace-pre-wrap text-sm text-crimson-700">{error}</p>
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
        <p className="text-xs text-muted">首次启动可能需要一两分钟，请稍候</p>
      </div>
    );
  }

  return <App />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <HashRouter>
      <Bootstrap />
    </HashRouter>
  </StrictMode>
);
