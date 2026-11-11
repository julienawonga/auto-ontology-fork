// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import { buildChatBody, consumeSseStream } from '@/api/chat';
import type { ClarificationEvent } from '@/types/chat';
import {
	CLARIFICATION_TEXT_MAX_LENGTH,
	nextOptionIndex,
	normalizeClarificationText,
	optionReply,
	parseClarificationEvent,
	parseClarificationFence,
	answerFromUserText,
	clarificationRefusalMessage,
	textReply,
	toClarificationPart,
} from './clarification';
import {
	clarificationEventFixture,
	clarificationHistoryFixture,
	clarificationSseFixture,
} from './clarification.fixture';

describe('parseClarificationEvent', () => {
	it('accepts the contract event', () => {
		expect(parseClarificationEvent(clarificationEventFixture)).toEqual(
			clarificationEventFixture,
		);
	});

	it.each([
		['wrong type', { ...clarificationEventFixture, type: 'step' }],
		['no id', { ...clarificationEventFixture, clarification_id: '' }],
		['no question', { ...clarificationEventFixture, question: ' ' }],
		[
			'no options and no free text',
			{ ...clarificationEventFixture, options: [], allow_other: false },
		],
		['not an object', 'nope'],
	])('rejects %s', (_name, raw) => {
		expect(parseClarificationEvent(raw)).toBeNull();
	});

	it('drops malformed options and caps the list at four', () => {
		const options = [
			{ id: 'a', label: 'A' },
			{ id: '', label: 'no id' },
			{ id: 'b', label: 'B' },
			{ id: 'c', label: 'C' },
			{ id: 'd', label: 'D' },
			{ id: 'e', label: 'E' },
		];
		const parsed = parseClarificationEvent({ ...clarificationEventFixture, options });
		expect(parsed?.options.map((o) => o.id)).toEqual(['a', 'b', 'c', 'd']);
	});
});

describe('stream', () => {
	it('delivers the clarification event and then finishes', async () => {
		const received: ClarificationEvent[] = [];
		const order: string[] = [];
		await consumeSseStream(new Response(clarificationSseFixture), {
			onStep: () => order.push('step'),
			onResult: () => order.push('result'),
			onClarification: (event) => {
				order.push('clarification');
				received.push(event);
			},
			onError: () => order.push('error'),
			onDone: () => order.push('done'),
		});
		expect(received).toHaveLength(1);
		expect(received[0].clarification_id).toBe(clarificationEventFixture.clarification_id);
		expect(order).toEqual(['step', 'clarification', 'done']);
	});

	it('skips a malformed clarification event without failing the stream', async () => {
		const bad = { type: 'clarification', clarification_id: 'x' };
		const order: string[] = [];
		await consumeSseStream(new Response(`data: ${JSON.stringify(bad)}\n\ndata: [DONE]\n\n`), {
			onStep: () => {},
			onResult: () => {},
			onClarification: () => order.push('clarification'),
			onError: () => order.push('error'),
			onDone: () => order.push('done'),
		});
		expect(order).toEqual(['done']);
	});
});

describe('answer payload', () => {
	it('sends a chosen option', () => {
		const body = JSON.parse(
			buildChatBody({
				question: 'Top 10% by total payments',
				conversationId: 'conv-1',
				clarification: optionReply('0b6f5a4e-6a1c-4c53-9a0e-2f6f0f0f9f2e', 'opt-2'),
			}),
		);
		expect(body).toEqual({
			question: 'Top 10% by total payments',
			conversation_id: 'conv-1',
			supports_clarification: true,
			clarification: {
				clarification_id: '0b6f5a4e-6a1c-4c53-9a0e-2f6f0f0f9f2e',
				option_id: 'opt-2',
			},
		});
	});

	it('sends free text', () => {
		expect(
			textReply('0b6f5a4e-6a1c-4c53-9a0e-2f6f0f0f9f2e', 'at least 15 in payments'),
		).toEqual({
			clarification_id: '0b6f5a4e-6a1c-4c53-9a0e-2f6f0f0f9f2e',
			text: 'at least 15 in payments',
		});
	});

	it('always opts in, and leaves the answer out of an ordinary question', () => {
		const body = JSON.parse(buildChatBody({ question: 'hi', conversationId: 'c' }));
		expect(body.supports_clarification).toBe(true);
		expect('clarification' in body).toBe(false);
	});
});

describe('normalizeClarificationText', () => {
	it('trims, rejects blank and caps the length', () => {
		expect(normalizeClarificationText('  15 payments \n')).toBe('15 payments');
		expect(normalizeClarificationText('   \n ')).toBeNull();
		const long = normalizeClarificationText('a'.repeat(CLARIFICATION_TEXT_MAX_LENGTH + 50));
		expect(long).toHaveLength(CLARIFICATION_TEXT_MAX_LENGTH);
	});
});

describe('nextOptionIndex', () => {
	it('moves with the arrows, wraps, and ignores other keys', () => {
		expect(nextOptionIndex('ArrowDown', 0, 4)).toBe(1);
		expect(nextOptionIndex('ArrowDown', 3, 4)).toBe(0);
		expect(nextOptionIndex('ArrowUp', 0, 4)).toBe(3);
		expect(nextOptionIndex('ArrowRight', 1, 4)).toBe(2);
		expect(nextOptionIndex('ArrowLeft', 1, 4)).toBe(0);
		expect(nextOptionIndex('Home', 2, 4)).toBe(0);
		expect(nextOptionIndex('End', 0, 4)).toBe(3);
		expect(nextOptionIndex('Enter', 0, 4)).toBeNull();
		expect(nextOptionIndex('ArrowDown', 0, 0)).toBeNull();
	});
});

describe('history adapter', () => {
	it('reads the fenced block and shows only the question text', () => {
		const parsed = parseClarificationFence(clarificationHistoryFixture);
		expect(parsed?.text).toBe('What do you mean by VIP customers?');
		expect(parsed?.part).toEqual(toClarificationPart(clarificationEventFixture));
	});

	it('ignores content without the block or with broken JSON', () => {
		expect(parseClarificationFence('plain answer')).toBeNull();
		expect(parseClarificationFence('q\n\n```clarification\n{nope\n```')).toBeNull();
	});

	it('matches the next user message to an option label', () => {
		const part = toClarificationPart(clarificationEventFixture);
		expect(answerFromUserText(part, 'Top 10% by total payments ').answer).toEqual({
			optionId: 'opt-2',
			text: 'Top 10% by total payments',
		});
	});

	it('shows an unmatched user message as a free-text answer', () => {
		const part = toClarificationPart(clarificationEventFixture);
		expect(answerFromUserText(part, 'above 100 EUR').answer).toEqual({
			optionId: undefined,
			text: 'above 100 EUR',
		});
	});

	it('turns a 422 detail into a message', () => {
		expect(clarificationRefusalMessage('Clarification already answered')).toContain(
			'no longer',
		);
		expect(clarificationRefusalMessage('option_id was not offered')).toContain(
			'could not be used',
		);
	});
});
