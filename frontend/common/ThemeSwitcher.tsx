// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { useLayoutEffect, useSyncExternalStore } from 'react';
import { ThemePreference } from '@/enums/theme';
import {
	readThemePreference,
	setThemePreference,
	subscribeToThemePreference,
	syncThemePreference,
} from '@/lib/theme';

const OPTIONS: { value: ThemePreference; label: string }[] = [
	{ value: ThemePreference.System, label: 'System' },
	{ value: ThemePreference.Light, label: 'Light' },
	{ value: ThemePreference.Dark, label: 'Dark' },
];

/** The stored preference; System during server rendering and hydration. */
const useThemePreference = (): ThemePreference =>
	useSyncExternalStore(
		subscribeToThemePreference,
		readThemePreference,
		() => ThemePreference.System,
	);

/**
 * Keeps the theme current for the whole session: follows the OS while on
 * System and picks up changes from other tabs. Mounted once in the root
 * layout, on every page including login, so it does not depend on the user
 * menu being open. Renders nothing.
 *
 * A layout effect, so the class is back before paint after React's dev-mode
 * Strict remount resets <html> to its JSX attributes (dropping the class the
 * inline script set). In production it only re-applies the same value.
 */
export const ThemeSync = () => {
	useLayoutEffect(() => syncThemePreference(), []);
	return null;
};

/** System / Light / Dark segmented control, shown in the user menu. */
export const ThemeSwitcher = () => {
	const preference = useThemePreference();

	return (
		<div className="px-3 py-1">
			<p className="mb-1.5 text-xs text-secondary dark:text-zinc-500">Theme</p>
			<div
				role="radiogroup"
				aria-label="Theme"
				className="flex rounded-md bg-zinc-100 p-0.5 dark:bg-zinc-800"
			>
				{OPTIONS.map(({ value, label }) => {
					const selected = preference === value;
					return (
						<button
							key={value}
							type="button"
							role="radio"
							aria-checked={selected}
							onClick={() => setThemePreference(value)}
							className={`flex-1 rounded px-2 py-1 text-xs font-medium transition-colors ${
								selected
									? 'bg-white text-heading shadow-sm dark:bg-zinc-950 dark:text-zinc-100'
									: 'text-secondary hover:text-heading dark:text-zinc-400 dark:hover:text-zinc-100'
							}`}
						>
							{label}
						</button>
					);
				})}
			</div>
		</div>
	);
};
