// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import {
	ONTOLOGY_LANGUAGE_OPTIONS,
	ontologyLanguageBadge,
	ontologyLanguageOptionsFor,
} from './ontologyLanguage';

describe('ontologyLanguage', () => {
	it('lists English first as the default', () => {
		expect(ONTOLOGY_LANGUAGE_OPTIONS[0].code).toBe('en');
		expect(ONTOLOGY_LANGUAGE_OPTIONS).toHaveLength(7);
	});

	it('upper-cases the badge and defaults a missing value to EN', () => {
		expect(ontologyLanguageBadge('fr')).toBe('FR');
		expect(ontologyLanguageBadge(undefined)).toBe('EN');
		expect(ontologyLanguageBadge('')).toBe('EN');
	});

	it('keeps a stored code outside the short list selectable', () => {
		expect(ontologyLanguageOptionsFor('fr')).toBe(ONTOLOGY_LANGUAGE_OPTIONS);
		const options = ontologyLanguageOptionsFor('ja');
		expect(options).toHaveLength(8);
		expect(options.at(-1)).toEqual({ code: 'ja', label: 'JA' });
	});
});
