// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

// A canned clarification event, so the card and the stream parser can be
// exercised without the backend (tests, and pasting into a dev tool).

import type { ClarificationEvent } from '@/types/chat';

export const clarificationEventFixture: ClarificationEvent = {
	type: 'clarification',
	clarification_id: '0b6f5a4e-6a1c-4c53-9a0e-2f6f0f0f9f2e',
	term: 'VIP customers',
	question: 'What do you mean by VIP customers?',
	options: [
		{
			id: 'opt-1',
			label: 'Total payments above the average',
			description: "Compares each customer's total to the mean",
		},
		{
			id: 'opt-2',
			label: 'Top 10% by total payments',
			description: 'Ranked by what each customer paid',
		},
		{ id: 'opt-3', label: 'Above a fixed amount' },
	],
	allow_other: true,
};

/** How history stores the same question: its text, then a fenced JSON block. */
export const clarificationHistoryFixture =
	`${clarificationEventFixture.question}\n\n` +
	'```clarification\n' +
	JSON.stringify({
		...clarificationEventFixture,
		type: undefined,
		original_question: 'How many VIP customers do we have?',
	}) +
	'\n```';

/** The same event as the backend frames it on the wire, followed by `[DONE]`. */
export const clarificationSseFixture =
	`data: ${JSON.stringify({ type: 'step', node: 'intent', label: 'Validating intent' })}\n\n` +
	`data: ${JSON.stringify(clarificationEventFixture)}\n\n` +
	'data: [DONE]\n\n';
