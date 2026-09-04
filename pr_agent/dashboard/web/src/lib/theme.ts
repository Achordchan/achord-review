export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'dashboard-theme'

/** The active theme: a stored choice, else whatever the pre-paint script set, else dark. */
export function getStoredTheme(): Theme {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    if (v === 'light' || v === 'dark') return v
  } catch {
    // localStorage unavailable (private mode, blocked) — fall back to the DOM/default.
  }
  const attr = document.documentElement.getAttribute('data-theme')
  return attr === 'light' ? 'light' : 'dark'
}

/** Apply a theme to the document and remember it. */
export function setTheme(theme: Theme): void {
  document.documentElement.setAttribute('data-theme', theme)
  try {
    document.documentElement.style.colorScheme = theme
  } catch {
    // ignore
  }
  try {
    localStorage.setItem(STORAGE_KEY, theme)
  } catch {
    // preference simply won't persist this session
  }
}
