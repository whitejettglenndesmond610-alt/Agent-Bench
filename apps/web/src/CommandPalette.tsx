import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Kbd } from "./components";

export type CommandItem = {
  to: string;
  label: string;
  code: string;
  keywords?: string;
};

export function CommandPalette({
  open,
  items,
  onClose
}: {
  open: boolean;
  items: CommandItem[];
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return items;
    return items.filter((item) =>
      `${item.label} ${item.code} ${item.keywords ?? ""}`.toLowerCase().includes(needle)
    );
  }, [items, query]);

  useEffect(() => {
    if (open) {
      setQuery("");
      setActiveIndex(0);
      inputRef.current?.focus();
    }
  }, [open]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const go = (item: CommandItem | undefined) => {
    if (!item) return;
    onClose();
    navigate(item.to);
  };

  return (
    <div
      className="command-palette-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section className="command-palette" role="dialog" aria-modal="true" aria-label="命令面板">
        <div className="command-palette-input">
          <input
            ref={inputRef}
            type="text"
            value={query}
            placeholder="搜索页面或功能…"
            aria-label="搜索页面"
            role="combobox"
            aria-expanded="true"
            aria-controls="command-palette-list"
            aria-activedescendant={filtered[activeIndex] ? `command-option-${activeIndex}` : undefined}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setActiveIndex((index) => Math.min(index + 1, filtered.length - 1));
              } else if (event.key === "ArrowUp") {
                event.preventDefault();
                setActiveIndex((index) => Math.max(index - 1, 0));
              } else if (event.key === "Enter") {
                event.preventDefault();
                go(filtered[activeIndex]);
              }
            }}
          />
          <Kbd>Esc</Kbd>
        </div>
        <ul className="command-palette-list" role="listbox" id="command-palette-list" aria-label="页面跳转">
          {filtered.map((item, index) => (
            <li key={item.to}>
              <button
                type="button"
                id={`command-option-${index}`}
                role="option"
                aria-selected={index === activeIndex}
                className={index === activeIndex ? "is-active" : ""}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => go(item)}
              >
                <strong>{item.label}</strong>
                <small>{item.code}</small>
              </button>
            </li>
          ))}
          {filtered.length === 0 && <li className="command-palette-empty">没有匹配的页面</li>}
        </ul>
        <footer className="command-palette-hint">
          <span><Kbd>↑</Kbd><Kbd>↓</Kbd> 选择</span>
          <span><Kbd>↵</Kbd> 跳转</span>
          <span><Kbd>Esc</Kbd> 关闭</span>
        </footer>
      </section>
    </div>
  );
}
