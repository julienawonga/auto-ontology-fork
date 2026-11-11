// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { toClarificationPart } from '@/lib/clarification';
import { clarificationEventFixture } from '@/lib/clarification.fixture';
import { ClarificationCard } from './ClarificationCard';

// No DOM is available here, so these check the markup each state renders;
// keyboard movement is covered by `nextOptionIndex` in lib/clarification.test.ts.
const part = toClarificationPart(clarificationEventFixture);
const onAnswer = async () => true;

describe('ClarificationCard', () => {
	it('renders a pending question as a radio group with an Other row', () => {
		const html = renderToStaticMarkup(
			<ClarificationCard part={part} isLatest onAnswer={onAnswer} />,
		);
		expect(html).toContain('What do you mean by VIP customers?');
		expect(html).toContain('role="radiogroup"');
		expect(html.match(/role="radio"/g)).toHaveLength(4);
		expect(html).toContain('Total payments above the average');
		expect(html).toContain('Ranked by what each customer paid');
		expect(html).toContain('Other');
		// Roving tabindex: only the first row is in the tab order.
		expect(html.match(/tabindex="0"/g)).toHaveLength(1);
		expect(html).not.toContain('aria-disabled="true"');
	});

	it('omits the Other row when free text is not allowed', () => {
		const html = renderToStaticMarkup(
			<ClarificationCard
				part={{ ...part, allowOther: false }}
				isLatest
				onAnswer={onAnswer}
			/>,
		);
		expect(html.match(/role="radio"/g)).toHaveLength(3);
	});

	it('disables the options while a turn is streaming', () => {
		const html = renderToStaticMarkup(
			<ClarificationCard part={part} isLatest disabled onAnswer={onAnswer} />,
		);
		expect(html.match(/aria-disabled="true"/g)).toHaveLength(4);
	});

	it('renders an answered card compactly, without options', () => {
		const html = renderToStaticMarkup(
			<ClarificationCard
				part={{ ...part, answer: { optionId: 'opt-2', text: 'Top 10% by total payments' } }}
				isLatest
				onAnswer={onAnswer}
			/>,
		);
		expect(html).toContain('Clarification, answered');
		expect(html).toContain('Top 10% by total payments');
		expect(html).not.toContain('role="radio"');
		expect(html).not.toContain('<textarea');
	});

	it('shows a question the backend refused as closed', () => {
		const html = renderToStaticMarkup(
			<ClarificationCard part={{ ...part, closed: true }} isLatest onAnswer={onAnswer} />,
		);
		expect(html).toContain('This question was not answered.');
		expect(html).not.toContain('role="radio"');
	});

	it('shows an unanswered question that is no longer the latest as stale', () => {
		const html = renderToStaticMarkup(
			<ClarificationCard part={part} isLatest={false} onAnswer={onAnswer} />,
		);
		expect(html).toContain('This question was not answered.');
		expect(html).not.toContain('role="radio"');
	});
});
