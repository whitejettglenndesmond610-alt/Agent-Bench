import { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes, useEffect, useRef, useState } from "react";

type ButtonVariant = "primary" | "ghost" | "danger";
type ButtonSize = "sm" | "md";

export function Button({
  className = "",
  variant = "primary",
  size = "md",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; size?: ButtonSize }) {
  const classes = [
    "button",
    variant !== "primary" ? `button--${variant}` : "",
    size !== "md" ? `button--${size}` : "",
    className
  ].filter(Boolean).join(" ");
  return <button className={classes} {...props} />;
}

export function Input({ className = "", ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={`control ${className}`.trim()} {...props} />;
}

export function Textarea({ className = "", ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={`control textarea ${className}`.trim()} {...props} />;
}

export function Select({ className = "", ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={`control select ${className}`.trim()} {...props} />;
}

export function FormField({
  label,
  htmlFor,
  hint,
  error,
  required,
  children
}: {
  label: string;
  htmlFor: string;
  hint?: string;
  error?: string;
  required?: boolean;
  children: ReactNode;
}) {
  return (
    <div className={`form-field ${error ? "has-error" : ""}`}>
      <label htmlFor={htmlFor}>
        {label}
        {required && <span aria-hidden="true"> *</span>}
      </label>
      {children}
      {error ? <small className="field-error">{error}</small> : hint && <small>{hint}</small>}
    </div>
  );
}

export function Table({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`table-frame ${className}`.trim()}><table>{children}</table></div>;
}

export type StatusTone = "success" | "warning" | "danger" | "info" | "neutral";

export function StatusPill({ tone = "neutral", children }: { tone?: StatusTone; children: ReactNode }) {
  return (
    <span className={`status-pill status-pill--${tone}`}>
      <i className="status-pill-dot" aria-hidden="true" />
      {children}
    </span>
  );
}

export function Skeleton({
  width,
  height = 12,
  lines = 1,
  className = ""
}: {
  width?: number | string;
  height?: number;
  lines?: number;
  className?: string;
}) {
  const style = { width, height };
  if (lines <= 1) {
    return <span className={`skeleton ${className}`.trim()} style={style} aria-hidden="true" />;
  }
  return (
    <span className={`skeleton-stack ${className}`.trim()} aria-hidden="true">
      {Array.from({ length: lines }, (_, index) => (
        <span key={index} className="skeleton" style={{ ...style, width: index === lines - 1 && width === undefined ? "72%" : width }} />
      ))}
    </span>
  );
}

export type ToastMessage = {
  id: string;
  title: string;
  description?: string;
  tone?: StatusTone;
};

export function ToastViewport({ toasts, onDismiss }: { toasts: ToastMessage[]; onDismiss?: (id: string) => void }) {
  if (toasts.length === 0) return null;
  return (
    <div className="toast-viewport" role="status" aria-live="polite">
      {toasts.map((toast) => (
        <div key={toast.id} className={`toast toast--${toast.tone ?? "neutral"}`}>
          <i className="toast-dot" aria-hidden="true" />
          <div className="toast-copy">
            <strong>{toast.title}</strong>
            {toast.description && <span>{toast.description}</span>}
          </div>
          {onDismiss && (
            <button type="button" className="toast-dismiss" aria-label="关闭提示" onClick={() => onDismiss(toast.id)}>×</button>
          )}
        </div>
      ))}
    </div>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

/* Phase 2 — dense data-tool page primitives. */

export function PageHeader({
  title,
  meta,
  actions,
  className = ""
}: {
  title: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <header className={`page-header ${className}`.trim()}>
      <div className="page-header-main">
        <h1>{title}</h1>
        {meta && <div className="page-header-meta">{meta}</div>}
      </div>
      {actions && <div className="page-header-actions">{actions}</div>}
    </header>
  );
}

export function Tabs<T extends string>({
  items,
  active,
  onChange,
  ariaLabel,
  className = ""
}: {
  items: Array<{ key: T; label: ReactNode; hint?: ReactNode }>;
  active: T;
  onChange: (key: T) => void;
  ariaLabel: string;
  className?: string;
}) {
  return (
    <nav className={`seg-tabs ${className}`.trim()} aria-label={ariaLabel}>
      {items.map((item) => (
        <button
          key={item.key}
          type="button"
          className={item.key === active ? "is-active" : ""}
          aria-pressed={item.key === active}
          onClick={() => onChange(item.key)}
        >
          {item.hint && <small>{item.hint}</small>}
          {item.label}
        </button>
      ))}
    </nav>
  );
}

function prefersReducedMotion() {
  return typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function CountUp({
  value,
  format = (current: number) => String(Math.round(current)),
  duration = 600,
  className = ""
}: {
  value: number;
  format?: (current: number) => string;
  duration?: number;
  className?: string;
}) {
  const animate = typeof requestAnimationFrame === "function" && !prefersReducedMotion();
  const [display, setDisplay] = useState(() => (animate ? 0 : value));
  const previous = useRef(animate ? 0 : value);

  useEffect(() => {
    const from = previous.current;
    previous.current = value;
    if (from === value || typeof requestAnimationFrame !== "function" || prefersReducedMotion()) {
      setDisplay(value);
      return;
    }
    let frame = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const progress = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      setDisplay(from + (value - from) * eased);
      if (progress < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [value, duration]);

  return <span className={`count-up ${className}`.trim()}>{format(display)}</span>;
}

export function ProgressBar({
  value,
  label,
  active = false,
  className = ""
}: {
  value?: number;
  label?: string;
  active?: boolean;
  className?: string;
}) {
  const width = value === undefined ? 100 : Math.max(0, Math.min(100, value));
  return (
    <div
      className={`progress ${active ? "is-active" : ""} ${className}`.trim()}
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={value === undefined ? undefined : Math.round(width)}
    >
      <i style={{ width: `${width}%` }} />
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
  className = ""
}: {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={`empty-state grain ${className}`.trim()}>
      <strong>{title}</strong>
      {description && <p>{description}</p>}
      {action && <div className="empty-state-action">{action}</div>}
    </div>
  );
}

export function LoadingState({
  label,
  compact = false
}: {
  label: string;
  compact?: boolean;
}) {
  return (
    <div className={`loading-state ${compact ? "is-compact" : ""}`.trim()} role="progressbar" aria-label={label}>
      <span className="loading-spinner" aria-hidden="true" />
      <div className="loading-copy"><strong>{label}</strong><span>正在同步最新数据，请稍候</span></div>
      <div className="loading-bars" aria-hidden="true"><i /><i /><i /></div>
    </div>
  );
}

export function Modal({
  title,
  description,
  eyebrow = "AGENT CONFIGURATION",
  onClose,
  children
}: {
  title: string;
  description?: string;
  eyebrow?: string;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <div className="modal-backdrop" role="presentation">
      <section
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="modal-title"
      >
        <header className="modal-header">
          <div>
            <p className="eyebrow">{eyebrow}</p>
            <h2 id="modal-title">{title}</h2>
            {description && <p>{description}</p>}
          </div>
          <button type="button" className="modal-close" aria-label="关闭" onClick={onClose}>×</button>
        </header>
        {children}
      </section>
    </div>
  );
}
