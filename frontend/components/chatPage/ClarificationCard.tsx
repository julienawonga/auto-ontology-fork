// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { Spinner } from '@/common/Spinner';
import {
	CLARIFICATION_TEXT_MAX_LENGTH,
	nextOptionIndex,
	normalizeClarificationText,
	optionReply,
	textReply,
} from '@/lib/clarification';
import type { ClarificationPart, ClarificationReply } from '@/types/chat';
import { glassFocus, glassHover, glassPrimaryButton, glassSurface } from './glass';

type ClarificationCardProps = {
	part: ClarificationPart;
	/**
	 * Only the newest message can still be answered: once the conversation has
	 * moved on (the user typed a question instead, or the page was reloaded
	 * after the run) the question is stale and shown as such.
	 */
	isLatest: boolean;
	/** A turn is streaming, so a new one cannot be sent yet. */
	disabled?: boolean;
	/** Sends the answer as the next turn; resolves false when it was refused. */
	onAnswer: (text: string, reply: ClarificationReply) => Promise<boolean>;
};

// Stands in for an option id when the free-text row is the one being sent.
const OTHER = 'other';

const optionClass = `flex w-full min-w-0 cursor-pointer items-start gap-3 rounded-xl px-3 py-2.5 text-left text-sm text-body transition-colors aria-disabled:cursor-not-allowed aria-disabled:opacity-60 dark:text-zinc-300 ${glassHover} ${glassFocus}`;

const Question = ({ id, part }: { id: string; part: ClarificationPart }) => (
	<div className="min-w-0">
		{part.term && (
			<p className="text-[11px] font-semibold tracking-wide text-secondary uppercase dark:text-zinc-400">
				{part.term}
			</p>
		)}
		<p id={id} className="text-sm font-medium break-words text-glass-text dark:text-zinc-50">
			{part.question}
		</p>
	</div>
);

export const ClarificationCard = ({
	part,
	isLatest,
	disabled = false,
	onAnswer,
}: ClarificationCardProps) => {
	const questionId = useId();
	const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
	const textRef = useRef<HTMLTextAreaElement>(null);
	const [activeIndex, setActiveIndex] = useState(0);
	const [otherOpen, setOtherOpen] = useState(false);
	const [text, setText] = useState('');
	// What was chosen while the next turn is being accepted.
	const [submitting, setSubmitting] = useState<string | null>(null);

	const pending = !part.answer && !part.closed && isLatest;

	// A new question takes focus so a keyboard or screen-reader user lands on it
	// without hunting for it below the thread.
	useEffect(() => {
		if (pending) optionRefs.current[0]?.focus();
	}, [pending]);

	if (part.answer) {
		return (
			<div
				role="group"
				aria-label="Clarification, answered"
				className={`flex min-w-0 flex-col gap-1.5 rounded-2xl px-4 py-3 ${glassSurface}`}
			>
				<Question id={questionId} part={part} />
				<p className="flex min-w-0 items-start gap-2 text-sm text-body dark:text-zinc-300">
					<svg
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						strokeWidth={2.5}
						strokeLinecap="round"
						strokeLinejoin="round"
						aria-hidden="true"
						className="mt-0.5 h-4 w-4 shrink-0 text-glass-success"
					>
						<path d="M5 12l5 5L20 7" />
					</svg>
					<span className="sr-only">Your answer: </span>
					<span className="min-w-0 break-words">{part.answer.text}</span>
				</p>
			</div>
		);
	}

	if (!pending) {
		return (
			<div
				role="group"
				aria-label="Clarification, not answered"
				className={`flex min-w-0 flex-col gap-1.5 rounded-2xl px-4 py-3 ${glassSurface}`}
			>
				<Question id={questionId} part={part} />
				<p className="text-xs text-secondary italic dark:text-zinc-500">
					This question was not answered.
				</p>
			</div>
		);
	}

	const busy = submitting !== null;
	const locked = busy || disabled;
	// Rows: the listed options, then the free-text one when allowed.
	const rowCount = part.options.length + (part.allowOther ? 1 : 0);

	const send = async (choice: string, shown: string, reply: ClarificationReply) => {
		if (locked) return;
		setSubmitting(choice);
		const accepted = await onAnswer(shown, reply);
		// Accepted: the part gains its answer and this branch unmounts.
		if (!accepted) setSubmitting(null);
	};

	const submitText = () => {
		const answer = normalizeClarificationText(text);
		if (answer) void send(OTHER, answer, textReply(part.id, answer));
	};

	const focusRow = (index: number) => {
		setActiveIndex(index);
		optionRefs.current[index]?.focus();
	};

	const onOptionKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
		const next = nextOptionIndex(event.key, index, rowCount);
		if (next === null) return;
		event.preventDefault();
		focusRow(next);
	};

	const onTextKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
		if (event.key === 'Escape') {
			event.preventDefault();
			setOtherOpen(false);
			optionRefs.current[part.options.length]?.focus();
			return;
		}
		// Shift+Enter keeps the newline; Enter during IME composition confirms a character.
		if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
			event.preventDefault();
			submitText();
		}
	};

	const otherIndex = part.options.length;

	return (
		<div
			role="group"
			aria-labelledby={questionId}
			className={`flex min-w-0 flex-col gap-3 rounded-2xl p-4 ${glassSurface}`}
		>
			<Question id={questionId} part={part} />

			{/* Radio semantics so arrow keys move between rows; choosing one sends at once. */}
			<div
				role="radiogroup"
				aria-labelledby={questionId}
				aria-busy={busy}
				className="flex flex-col gap-1"
			>
				{part.options.map((option, index) => (
					<button
						key={option.id}
						ref={(el) => {
							optionRefs.current[index] = el;
						}}
						type="button"
						role="radio"
						aria-checked={submitting === option.id}
						aria-disabled={locked}
						tabIndex={index === activeIndex ? 0 : -1}
						onFocus={() => setActiveIndex(index)}
						onKeyDown={(event) => onOptionKeyDown(event, index)}
						onClick={() =>
							void send(option.id, option.label, optionReply(part.id, option.id))
						}
						className={optionClass}
					>
						<span
							aria-hidden="true"
							className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-secondary/60 dark:border-zinc-500"
						>
							{submitting === option.id && <Spinner className="h-3 w-3" />}
						</span>
						<span className="flex min-w-0 flex-col">
							<span className="font-medium break-words text-heading dark:text-zinc-100">
								{option.label}
							</span>
							{option.description && (
								<span className="text-xs break-words text-secondary dark:text-zinc-400">
									{option.description}
								</span>
							)}
						</span>
					</button>
				))}

				{part.allowOther && (
					<button
						ref={(el) => {
							optionRefs.current[otherIndex] = el;
						}}
						type="button"
						role="radio"
						aria-checked={otherOpen}
						aria-disabled={locked}
						tabIndex={otherIndex === activeIndex ? 0 : -1}
						onFocus={() => setActiveIndex(otherIndex)}
						onKeyDown={(event) => onOptionKeyDown(event, otherIndex)}
						onClick={() => {
							if (locked) return;
							setOtherOpen(true);
							// The textarea mounts on this render; focus it after.
							requestAnimationFrame(() => textRef.current?.focus());
						}}
						className={optionClass}
					>
						<span
							aria-hidden="true"
							className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-secondary/60 dark:border-zinc-500"
						>
							{submitting === OTHER && <Spinner className="h-3 w-3" />}
						</span>
						<span className="font-medium text-heading dark:text-zinc-100">Other</span>
					</button>
				)}
			</div>

			{part.allowOther && otherOpen && (
				<div className="flex items-end gap-2">
					<textarea
						ref={textRef}
						rows={2}
						value={text}
						maxLength={CLARIFICATION_TEXT_MAX_LENGTH}
						aria-label="Your own answer"
						placeholder="Type your answer…"
						readOnly={locked}
						onChange={(event) => setText(event.target.value)}
						onKeyDown={onTextKeyDown}
						className={`min-w-0 flex-1 resize-none rounded-xl bg-transparent px-3 py-2 text-sm text-heading placeholder:text-secondary dark:text-zinc-100 dark:placeholder:text-zinc-500 ${glassSurface} ${glassFocus}`}
					/>
					<button
						type="button"
						aria-label="Send answer"
						aria-disabled={locked || !normalizeClarificationText(text)}
						onClick={submitText}
						className={`flex h-9 w-9 shrink-0 cursor-pointer items-center justify-center rounded-full aria-disabled:cursor-not-allowed aria-disabled:opacity-50 ${glassPrimaryButton} ${glassFocus}`}
					>
						<svg
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							strokeWidth={2.5}
							strokeLinecap="round"
							strokeLinejoin="round"
							aria-hidden="true"
							className="h-4 w-4"
						>
							<path d="M12 19V5M5 12l7-7 7 7" />
						</svg>
					</button>
				</div>
			)}
		</div>
	);
};
