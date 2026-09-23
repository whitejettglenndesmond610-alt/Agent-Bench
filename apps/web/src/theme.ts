import { useCallback, useState } from "react";

export type Theme = "dark" | "light";

export const THEME_STORAGE_KEY = "agent-bench:theme";

export function getStoredTheme(): Theme {
  try {
    return window.localStorage.getItem(THEME_STORAGE_KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
}

export function useTheme(): { theme: Theme; toggleTheme: () => void } {
  const [theme, setTheme] = useState<Theme>(() => getStoredTheme());
  const toggleTheme = useCallback(() => {
    setTheme((current) => {
      const next = current === "dark" ? "light" : "dark";
      applyTheme(next);
      try {
        window.localStorage.setItem(THEME_STORAGE_KEY, next);
      } catch {
        // Storage may be unavailable (private mode); the attribute still updates.
      }
      return next;
    });
  }, []);
  return { theme, toggleTheme };
}
