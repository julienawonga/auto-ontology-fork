// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

/**
 * Light/dark theme: the user's choice, and the `dark` class on <html> that
 * Tailwind's `dark:` variant and the dark rules in `globals.css` key off.
 *
 * The choice is per browser (localStorage), not per account: it is a viewing
 * preference with nothing to sync. Storage can be unavailable (private mode,
 * blocked site data), so every access is guarded and falls back to System.
 *
 * The class is first set by `THEME_INIT_SCRIPT`, inlined in the root layout so
 * it runs before the first paint; after that this module keeps it current.
 */

import { ThemePreference } from '@/enums/theme';

export const THEME_STORAGE_KEY = 'auto-ontology-theme';

const DARK_CLASS = 'dark';
const DARK_QUERY = '(prefers-color-scheme: dark)';
/** Dispatched on `window` when this tab changes the preference. */
const CHANGE_EVENT = 'auto-ontology-theme-change';

const isPreference = (value: unknown): value is ThemePreference =>
	Object.values(ThemePreference).includes(value as ThemePreference);

export const readThemePreference = (): ThemePreference => {
	try {
		const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
		return isPreference(stored) ? stored : ThemePreference.System;
	} catch {
		return ThemePreference.System;
	}
};

const systemPrefersDark = (): boolean => window.matchMedia(DARK_QUERY).matches;

/** Put the `dark` class on <html> to match `preference`. */
export const applyThemePreference = (preference: ThemePreference): void => {
	const dark =
		preference === ThemePreference.Dark ||
		(preference === ThemePreference.System && systemPrefersDark());
	document.documentElement.classList.toggle(DARK_CLASS, dark);
	document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
};

/** Save, apply, and announce a new preference. */
export const setThemePreference = (preference: ThemePreference): void => {
	try {
		if (preference === ThemePreference.System) {
			window.localStorage.removeItem(THEME_STORAGE_KEY);
		} else {
			window.localStorage.setItem(THEME_STORAGE_KEY, preference);
		}
	} catch {
		// Not persisted, but still applied for this page view.
	}
	applyThemePreference(preference);
	window.dispatchEvent(new Event(CHANGE_EVENT));
};

/**
 * Keep the <html> class in step with the preference: follows the OS while on
 * System, and picks up a change made in another tab. Returns the cleanup.
 */
export const syncThemePreference = (): (() => void) => {
	const query = window.matchMedia(DARK_QUERY);
	const reapply = () => applyThemePreference(readThemePreference());
	const onStorage = (event: StorageEvent) => {
		if (event.key === THEME_STORAGE_KEY || event.key === null) {
			reapply();
			window.dispatchEvent(new Event(CHANGE_EVENT));
		}
	};
	reapply();
	query.addEventListener('change', reapply);
	window.addEventListener('storage', onStorage);
	return () => {
		query.removeEventListener('change', reapply);
		window.removeEventListener('storage', onStorage);
	};
};

/** Subscribe to preference changes (for `useSyncExternalStore`). */
export const subscribeToThemePreference = (onChange: () => void): (() => void) => {
	window.addEventListener(CHANGE_EVENT, onChange);
	return () => window.removeEventListener(CHANGE_EVENT, onChange);
};

/** Whether dark mode is in effect right now, whatever the reason. */
export const isDarkThemeActive = (): boolean =>
	typeof document !== 'undefined' && document.documentElement.classList.contains(DARK_CLASS);

/**
 * Call `onChange` with the effective dark/light state whenever it flips, for
 * code that paints colors itself (canvas) instead of through CSS.
 */
export const observeDarkTheme = (onChange: (dark: boolean) => void): (() => void) => {
	let dark = isDarkThemeActive();
	const observer = new MutationObserver(() => {
		const next = isDarkThemeActive();
		if (next === dark) return;
		dark = next;
		onChange(next);
	});
	observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
	return () => observer.disconnect();
};

/**
 * Runs inline in <head>, before the first paint, so a dark page never flashes
 * light (or the reverse). Self-contained: it cannot import anything, so it
 * repeats the storage key and the rules of `applyThemePreference`.
 */
export const THEME_INIT_SCRIPT = `(function(){var p=null;try{p=localStorage.getItem(${JSON.stringify(
	THEME_STORAGE_KEY,
)})}catch(_){}try{var d=p==='dark'||(p!=='light'&&window.matchMedia(${JSON.stringify(
	DARK_QUERY,
)}).matches);var e=document.documentElement;if(d)e.classList.add('dark');e.style.colorScheme=d?'dark':'light'}catch(_){}})();`;
