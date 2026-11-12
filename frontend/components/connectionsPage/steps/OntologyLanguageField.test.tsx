// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { OntologyLanguageField } from './OntologyLanguageField';

describe('OntologyLanguageField', () => {
	it('offers the seven languages with English as the default', () => {
		const html = renderToStaticMarkup(<OntologyLanguageField value="en" onChange={() => {}} />);
		expect(html.match(/<option/g)).toHaveLength(7);
		expect(html).toContain('English (default)');
		expect(html).toContain('questions in other languages are translated automatically');
		expect(html).not.toContain('does not rebuild');
	});

	it('shows the no-rebuild note only when editing, and preselects the stored value', () => {
		const html = renderToStaticMarkup(
			<OntologyLanguageField value="fr" onChange={() => {}} editing />,
		);
		expect(html).toContain('does not rebuild the existing glossary');
		expect(html).toMatch(/<option value="fr" selected/);
	});
});
