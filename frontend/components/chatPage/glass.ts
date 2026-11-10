// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

/**
 * Class recipes for the glass design trial on the Chat page.
 *
 * Kept in one place so the frosted look stays consistent between the
 * composer, the bubbles and the result cards, and can be dropped in one edit if
 * the trial is not kept. Colours come from the `glass-*` tokens in globals.css.
 */

/** A frosted panel that floats over the page: composer, user bubble, cards. */
export const glassSurface =
	'border border-white/70 bg-white/60 shadow-[0_8px_32px_rgba(24,86,255,0.10)] backdrop-blur-xl dark:border-white/10 dark:bg-white/[0.06] dark:shadow-[0_8px_32px_rgba(0,0,0,0.45)]';

/** Hover fill for rows and icon buttons sitting on glass. */
export const glassHover = 'hover:bg-white/70 dark:hover:bg-white/[0.08]';

/** Keyboard focus ring, visible on both themes. */
export const glassFocus =
	'outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-transparent dark:focus-visible:ring-primary-soft';

/** The one filled action: send. */
export const glassPrimaryButton =
	'bg-primary text-white transition-colors hover:bg-primary-hover disabled:bg-zinc-300/70 disabled:text-white/80 dark:disabled:bg-white/10 dark:disabled:text-zinc-500';
