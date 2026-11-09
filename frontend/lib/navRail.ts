// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

/**
 * Whether the left navigation is open (icons and labels) or collapsed (icons).
 *
 * Per browser (localStorage), like the theme: a layout preference with nothing
 * to sync. Every access is guarded, since storage can be unavailable; the
 * fallback is the collapsed rail.
 */

const STORAGE_KEY = 'auto-ontology-nav-open';
/** Dispatched on `window` when this tab changes the preference. */
const CHANGE_EVENT = 'auto-ontology-nav-open-change';

export const readNavOpen = (): boolean => {
	try {
		return window.localStorage.getItem(STORAGE_KEY) === 'true';
	} catch {
		return false;
	}
};

/** The server has no storage to read, so it always renders the rail collapsed. */
export const readNavOpenOnServer = (): boolean => false;

export const setNavOpen = (open: boolean): void => {
	try {
		if (open) window.localStorage.setItem(STORAGE_KEY, 'true');
		else window.localStorage.removeItem(STORAGE_KEY);
	} catch {
		// Storage is unavailable, so the choice cannot be kept and the rail
		// stays as it was.
	}
	window.dispatchEvent(new Event(CHANGE_EVENT));
};

/** Subscribe to changes from this tab and from others (for `useSyncExternalStore`). */
export const subscribeToNavOpen = (onChange: () => void): (() => void) => {
	const onStorage = (event: StorageEvent) => {
		if (event.key === STORAGE_KEY || event.key === null) onChange();
	};
	window.addEventListener(CHANGE_EVENT, onChange);
	window.addEventListener('storage', onStorage);
	return () => {
		window.removeEventListener(CHANGE_EVENT, onChange);
		window.removeEventListener('storage', onStorage);
	};
};
