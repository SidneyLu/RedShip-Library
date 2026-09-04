import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { cn } from "@/lib/utils";

type ToastItem = {
  id: number;
  message: string;
  variant: "info" | "success" | "error";
};

type ToastContextValue = {
  showToast: (message: string, variant?: ToastItem["variant"]) => void;
};

const ToastContext = createContext<ToastContextValue | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);

  const showToast = useCallback((message: string, variant: ToastItem["variant"] = "info") => {
    const id = Date.now() + Math.random();
    setItems((prev) => [...prev, { id, message, variant }]);
    window.setTimeout(() => {
      setItems((prev) => prev.filter((t) => t.id !== id));
    }, 4000);
  }, []);

  const value = useMemo(() => ({ showToast }), [showToast]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-[100] flex max-w-sm flex-col gap-2">
        {items.map((t) => (
          <div
            key={t.id}
            className={cn(
              "rounded-lg border px-4 py-2 text-sm shadow-lg backdrop-blur",
              t.variant === "error"
                ? "border-red-200 bg-red-50 text-red-800"
                : t.variant === "success"
                  ? "border-emerald-200 bg-emerald-50 text-emerald-900"
                  : "border-border bg-white/95 text-ink"
            )}
          >
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return ctx;
}
