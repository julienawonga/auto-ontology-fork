// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
	ActionBarPrimitive,
	AssistantRuntimeProvider,
	AuiIf,
	ComposerPrimitive,
	MessageNotSentError,
	MessagePrimitive,
	ThreadPrimitive,
	useExternalStoreRuntime,
	type AppendMessage,
	type ThreadMessageLike,
} from '@assistant-ui/react';
import type { ChatMessage, GraphStep } from '@/types/chat';
import { Icon, IconName } from '@/common/icons';
import { SkeletonBlock } from '@/common/Skeleton';
import { chatDisplayQueries } from '@/lib/queries/chatDisplay';
import { chatSuggestionQueries } from '@/lib/queries/chatSuggestions';
import { MessageBubble } from './MessageBubble';
import { ThinkingMessage } from './ThinkingMessage';

/**
 * The chat thread: messages, the composer and its suggestions.
 *
 * Rendering and input behaviour come from assistant-ui's unstyled primitives
 * (auto-growing composer, Enter to send, copy, suggestions, stick-to-bottom
 * scrolling). The messages themselves stay in `useChat`, which owns the stream
 * to the backend: assistant-ui only reads them through an external-store
 * runtime and hands new questions back through `onSend`.
 */

type ChatThreadProps = {
	messages: ChatMessage[];
	isLoading: boolean;
	steps: GraphStep[];
	/** SQL the running agent has produced but not finished executing. */
	liveSql?: string | null;
	messageListLoading?: boolean;
	/** Resolves false when the question was refused, so the draft is restored. */
	onSend: (text: string) => Promise<boolean>;
	onStop: () => void;
};

/** What the copy button puts on the clipboard for one message. */
const copyText = (message: ChatMessage): string => {
	const parts = [message.content.trim()];
	if (message.sql) parts.push(`\`\`\`sql\n${message.sql.trim()}\n\`\`\``);
	if (message.sqlResponse && !message.content.trim()) parts.push(message.sqlResponse);
	return parts.filter(Boolean).join('\n\n');
};

// The original message rides along in `metadata.custom`, so rendering keeps
// using our own components while assistant-ui sees plain text for copying.
const convertMessage = (message: ChatMessage): ThreadMessageLike => ({
	id: message.id,
	role: message.role,
	content: [{ type: 'text', text: copyText(message) }],
	createdAt: new Date(message.timestamp),
	metadata: { custom: { chatMessage: message } },
});

const textOf = (message: AppendMessage): string =>
	message.content
		.map((part) => (part.type === 'text' ? part.text : ''))
		.join('\n')
		.trim();

const iconButtonClass =
	'flex h-7 w-7 items-center justify-center rounded-md text-secondary transition-colors hover:bg-zinc-100 hover:text-heading disabled:opacity-40 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100';

const ThreadMessage = ({
	message,
	detailsExpandedByDefault,
}: {
	message: ChatMessage;
	detailsExpandedByDefault: boolean;
}) => (
	<MessagePrimitive.Root className="group flex flex-col gap-1">
		<MessageBubble message={message} detailsExpandedByDefault={detailsExpandedByDefault} />
		{message.role === 'assistant' && (
			<ActionBarPrimitive.Root autohide="not-last" className="flex gap-1">
				<ActionBarPrimitive.Copy
					copiedDuration={1500}
					aria-label="Copy answer"
					className={`${iconButtonClass} group/copy`}
				>
					<Icon
						name={IconName.Copy}
						className="h-3.5 w-3.5 group-data-[copied]/copy:hidden"
					/>
					<Icon
						name={IconName.Check}
						className="hidden h-3.5 w-3.5 text-[#76b900] group-data-[copied]/copy:block"
					/>
				</ActionBarPrimitive.Copy>
			</ActionBarPrimitive.Root>
		)}
	</MessagePrimitive.Root>
);

/** The question box. One instance at a time: centred on a new chat, docked below a thread. */
const Composer = () => (
	<div className="flex flex-col gap-2">
		<ComposerPrimitive.Root className="flex flex-col gap-2 rounded-3xl border border-zinc-300 bg-white px-4 pt-3 pb-2.5 shadow-sm transition-colors focus-within:border-[#76b900] dark:border-zinc-700 dark:bg-zinc-900 dark:focus-within:border-[#76b900]">
			<ComposerPrimitive.Input
				autoFocus
				rows={1}
				maxRows={10}
				placeholder="Ask a question about your data…"
				aria-label="Message"
				className="max-h-60 w-full resize-none bg-transparent text-[15px] leading-relaxed text-heading outline-none placeholder:text-secondary dark:text-zinc-100 dark:placeholder:text-zinc-500"
			/>
			<div className="flex justify-end">
				<AuiIf condition={(s) => !s.thread.isRunning}>
					<ComposerPrimitive.Send
						aria-label="Send message"
						className="flex h-8 w-8 items-center justify-center rounded-full bg-[#76b900] text-white transition-colors hover:bg-[#6aa600] disabled:bg-zinc-200 disabled:text-zinc-400 dark:disabled:bg-zinc-700 dark:disabled:text-zinc-500"
					>
						<Icon name={IconName.Send} className="h-4 w-4" />
					</ComposerPrimitive.Send>
				</AuiIf>
				<AuiIf condition={(s) => s.thread.isRunning}>
					<ComposerPrimitive.Cancel
						aria-label="Stop generation"
						className="flex h-8 w-8 items-center justify-center rounded-full bg-heading text-white transition-colors hover:opacity-80 dark:bg-zinc-100 dark:text-zinc-900"
					>
						<Icon name={IconName.Stop} className="h-3.5 w-3.5" />
					</ComposerPrimitive.Cancel>
				</AuiIf>
			</div>
		</ComposerPrimitive.Root>
		<p className="text-center text-[11px] text-secondary dark:text-zinc-500">
			Enter to send · Shift+Enter for a new line
		</p>
	</div>
);

/** Saved analyses offered as one-click questions, listed under the centred composer. */
const SuggestionList = ({ prompts }: { prompts: string[] }) =>
	prompts.length === 0 ? null : (
		<ul className="flex flex-col" aria-label="Suggested questions">
			{prompts.map((prompt) => (
				<li key={prompt}>
					<ThreadPrimitive.Suggestion
						prompt={prompt}
						send
						className="flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-sm text-body transition-colors hover:bg-zinc-100 hover:text-heading dark:text-zinc-300 dark:hover:bg-zinc-800/70 dark:hover:text-zinc-100"
					>
						<Icon
							name={IconName.ChartBar}
							className="h-4 w-4 shrink-0 text-secondary dark:text-zinc-500"
						/>
						{prompt}
					</ThreadPrimitive.Suggestion>
				</li>
			))}
		</ul>
	);

export const ChatThread = ({
	messages,
	isLoading,
	steps,
	liveSql,
	messageListLoading = false,
	onSend,
	onStop,
}: ChatThreadProps) => {
	// Collapsed while loading or if the read fails: the setting's default.
	const { data: detailsExpandedByDefault = false } = useQuery(
		chatDisplayQueries.detailsExpanded(),
	);
	const { data: suggestions = [] } = useQuery(chatSuggestionQueries.fromAnalyses());

	const onNew = useCallback(
		async (message: AppendMessage) => {
			const text = textOf(message);
			if (!text) return;
			// Hands the draft back to the composer, which cleared it on send.
			if (!(await onSend(text))) throw new MessageNotSentError('The question was refused.');
		},
		[onSend],
	);

	const onCancel = useCallback(async () => onStop(), [onStop]);

	const runtime = useExternalStoreRuntime<ChatMessage>({
		messages,
		isRunning: isLoading,
		convertMessage,
		onNew,
		onCancel,
	});

	// A new chat opens on the composer centred under a heading, with the
	// suggestions below it; the first question moves it down under the thread.
	const isNewChat = messages.length === 0 && !isLoading && !messageListLoading;

	return (
		<AssistantRuntimeProvider runtime={runtime}>
			<ThreadPrimitive.Root className="flex min-h-0 flex-1 flex-col">
				{isNewChat ? (
					<div className="flex min-h-0 flex-1 flex-col items-center justify-center overflow-y-auto px-4 pb-[12vh]">
						<div className="flex w-full max-w-3xl flex-col gap-6">
							<h1 className="text-center text-2xl font-medium text-balance text-heading dark:text-zinc-100">
								What would you like to know about your data?
							</h1>
							<Composer />
							<SuggestionList prompts={suggestions} />
						</div>
					</div>
				) : (
					<ThreadPrimitive.Viewport className="flex min-h-0 flex-1 flex-col overflow-y-auto">
						<div className="mx-auto w-full max-w-3xl flex-1 space-y-6 px-4 py-6">
							{messageListLoading ? (
								<div
									className="flex flex-col gap-4"
									role="status"
									aria-label="Loading messages"
								>
									<SkeletonBlock className="ml-auto h-12 w-1/2" />
									<SkeletonBlock className="h-20 w-3/5" />
									<SkeletonBlock className="h-16 w-2/3" />
								</div>
							) : (
								<>
									<ThreadPrimitive.Messages>
										{({ message }) => {
											const chatMessage = message.metadata.custom
												?.chatMessage as ChatMessage | undefined;
											// A placeholder the runtime adds while a run is
											// starting has no message of ours behind it; the
											// thinking indicator below covers that moment.
											if (!chatMessage) return null;
											return (
												<ThreadMessage
													message={chatMessage}
													detailsExpandedByDefault={
														detailsExpandedByDefault
													}
												/>
											);
										}}
									</ThreadPrimitive.Messages>
									{isLoading && (
										<ThinkingMessage steps={steps} liveSql={liveSql} />
									)}
								</>
							)}
						</div>

						<ThreadPrimitive.ViewportFooter className="sticky bottom-0 bg-gradient-to-t from-white from-70% to-transparent px-4 pt-4 pb-3 dark:from-zinc-950">
							<div className="relative mx-auto w-full max-w-3xl">
								<ThreadPrimitive.ScrollToBottom
									aria-label="Scroll to the latest message"
									className="absolute -top-11 left-1/2 flex h-8 w-8 -translate-x-1/2 items-center justify-center rounded-full border border-zinc-200 bg-white text-secondary shadow-sm transition-colors hover:text-heading disabled:invisible dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
								>
									<Icon
										name={IconName.ChevronRight}
										className="h-3.5 w-3.5 rotate-90"
									/>
								</ThreadPrimitive.ScrollToBottom>
								<Composer />
							</div>
						</ThreadPrimitive.ViewportFooter>
					</ThreadPrimitive.Viewport>
				)}
			</ThreadPrimitive.Root>
		</AssistantRuntimeProvider>
	);
};
