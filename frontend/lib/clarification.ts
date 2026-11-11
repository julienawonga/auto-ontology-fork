// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

// Pure helpers for clarification questions (stream parsing, the answer payload,
// keyboard navigation). Kept free of React so they can be unit-tested.

import type {
	ClarificationEvent,
	ClarificationOption,
	ClarificationPart,
	ClarificationReply,
} from '@/types/chat';

/** Longest free-text answer the card accepts. */
export const CLARIFICATION_TEXT_MAX_LENGTH = 500;

/** The card shows at most this many options; the contract promises 2 to 4. */
const MAX_OPTIONS = 4;

const isRecord = (value: unknown): value is Record<string, unknown> =>
	typeof value === 'object' && value !== null;

const nonEmptyString = (value: unknown): string | null =>
	typeof value === 'string' && value.trim() !== '' ? value : null;

const parseOptions = (raw: unknown): ClarificationOption[] => {
	if (!Array.isArray(raw)) return [];
	const options: ClarificationOption[] = [];
	for (const item of raw) {
		if (!isRecord(item)) continue;
		const id = nonEmptyString(item.id);
		const label = nonEmptyString(item.label);
		if (!id || !label) continue;
		const description = nonEmptyString(item.description) ?? undefined;
		options.push({ id, label, description });
	}
	return options.slice(0, MAX_OPTIONS);
};

/**
 * Validates a `clarification` stream event. Returns null for anything the card
 * could not render (no id, no question, no options and no free-text escape), so
 * a malformed event is dropped like any other malformed line instead of
 * leaving the user with a dead card.
 */
export const parseClarificationEvent = (raw: unknown): ClarificationEvent | null => {
	if (!isRecord(raw) || raw.type !== 'clarification') return null;
	const clarificationId = nonEmptyString(raw.clarification_id);
	const question = nonEmptyString(raw.question);
	if (!clarificationId || !question) return null;

	const options = parseOptions(raw.options);
	const allowOther = raw.allow_other !== false;
	if (options.length === 0 && !allowOther) return null;

	return {
		type: 'clarification',
		clarification_id: clarificationId,
		term: typeof raw.term === 'string' ? raw.term : '',
		question,
		options,
		allow_other: allowOther,
	};
};

export const toClarificationPart = (event: ClarificationEvent): ClarificationPart => ({
	id: event.clarification_id,
	term: event.term,
	question: event.question,
	options: event.options,
	allowOther: event.allow_other,
});

// --- Backend contract adapter -------------------------------------------------
// Everything that depends on how the backend frames clarifications lives here.

/**
 * History stores the question as an assistant message: the question text, then
 * a fenced ```clarification block holding the event as JSON (plus
 * `original_question`). Returns the part and the text to show, or null when
 * the message has no such block.
 */
export const parseClarificationFence = (
	content: string,
): { part: ClarificationPart; text: string } | null => {
	const match = /```clarification[ \t]*\r?\n([\s\S]*?)\r?\n?```/.exec(content);
	if (!match) return null;
	let raw: unknown;
	try {
		raw = JSON.parse(match[1]);
	} catch {
		return null;
	}
	const event = parseClarificationEvent(isRecord(raw) ? { type: 'clarification', ...raw } : raw);
	if (!event) return null;
	return {
		part: toClarificationPart(event),
		text: content.replace(match[0], '').trim() || event.question,
	};
};

/**
 * The answer shown on an answered card, from the user message that followed
 * it. No option id is stored, so the text is matched to an option label; with
 * no match it was typed in "Other".
 */
export const answerFromUserText = (part: ClarificationPart, text: string): ClarificationPart => {
	const trimmed = text.trim();
	const option = part.options.find((o) => o.label.trim() === trimmed);
	return { ...part, answer: { optionId: option?.id, text: trimmed } };
};

/**
 * The backend refuses an answer with HTTP 422 (stale or already answered
 * question, an option that was not offered, ...). Turns its `detail` into
 * something a user can act on.
 */
export const clarificationRefusalMessage = (detail: string): string =>
	/answered|stale|expired|no longer|unknown|not found|another conversation/i.test(detail)
		? 'This question can no longer be answered. Ask your question again.'
		: `That answer could not be used: ${detail.trim() || 'please ask your question again.'}`;

/** Trims a free-text answer; null when nothing is left to send. */
export const normalizeClarificationText = (text: string): string | null => {
	const trimmed = text.trim().slice(0, CLARIFICATION_TEXT_MAX_LENGTH).trim();
	return trimmed === '' ? null : trimmed;
};

/** A chosen option as the next request carries it. */
export const optionReply = (clarificationId: string, optionId: string): ClarificationReply => ({
	clarification_id: clarificationId,
	option_id: optionId,
});

/** A free-text answer as the next request carries it. */
export const textReply = (clarificationId: string, text: string): ClarificationReply => ({
	clarification_id: clarificationId,
	text,
});

/**
 * Where arrow keys move focus in the option list. Arrows wrap around; Home and
 * End jump to the ends. Returns null for keys that do not navigate.
 */
export const nextOptionIndex = (key: string, current: number, count: number): number | null => {
	if (count <= 0) return null;
	switch (key) {
		case 'ArrowDown':
		case 'ArrowRight':
			return (current + 1) % count;
		case 'ArrowUp':
		case 'ArrowLeft':
			return (current - 1 + count) % count;
		case 'Home':
			return 0;
		case 'End':
			return count - 1;
		default:
			return null;
	}
};
