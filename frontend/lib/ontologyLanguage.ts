// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

/** The language a database's ontology is written in (ISO 639-1). */
export const DEFAULT_ONTOLOGY_LANGUAGE = 'en';

/** Offered in the connection form; the backend accepts any two-letter code. */
export const ONTOLOGY_LANGUAGE_OPTIONS: ReadonlyArray<{ code: string; label: string }> = [
	{ code: 'en', label: 'English' },
	{ code: 'fr', label: 'French' },
	{ code: 'es', label: 'Spanish' },
	{ code: 'de', label: 'German' },
	{ code: 'it', label: 'Italian' },
	{ code: 'pt', label: 'Portuguese' },
	{ code: 'nl', label: 'Dutch' },
];

/** Badge text for the connections list, e.g. "EN"; a missing value reads as English. */
export const ontologyLanguageBadge = (code: string | null | undefined): string =>
	(code || DEFAULT_ONTOLOGY_LANGUAGE).toUpperCase();

/**
 * Options for the selector. A stored code outside the short list (set through
 * the API) is kept as its own entry so editing never silently changes it.
 */
export const ontologyLanguageOptionsFor = (
	current: string,
): ReadonlyArray<{ code: string; label: string }> =>
	ONTOLOGY_LANGUAGE_OPTIONS.some((option) => option.code === current)
		? ONTOLOGY_LANGUAGE_OPTIONS
		: [...ONTOLOGY_LANGUAGE_OPTIONS, { code: current, label: current.toUpperCase() }];
