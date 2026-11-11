// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

export type ChatMessage = {
	id: string;
	role: 'user' | 'assistant';
	content: string;
	sql?: string;
	sqlResponse?: string;
	thoughts?: string;
	/** Set when this assistant turn is a clarification question rather than an answer. */
	clarification?: ClarificationPart;
	timestamp: number;
};

export type ChatRequest = {
	question: string;
	/**
	 * Stable thread UUID. When supplied, prior completed turns are loaded and
	 * the new turn — including the chart/table bubble, once ready — is
	 * persisted to it. An unknown UUID creates the conversation. Omitting it
	 * runs the question statelessly, with no chart step or history.
	 */
	conversationId?: string | null;
	/** Scope retrieval/SQL to one connected database when multiple are loaded. */
	target_db?: string | null;
	/** Answers the clarification question the previous assistant turn asked. */
	clarification?: ClarificationReply;
};

/**
 * A graph node starting (`start`) or returning (`end`). Both are sent for
 * every node: the backend can only attach a node's `thought` once it has
 * finished, but waiting for that to show the label would leave the UI naming
 * the *previous* node for the whole time this one runs — a 20s reconstruction
 * displayed as "Validating intent". `phase` is absent on older backends, in
 * which case an event is treated as a plain "next step" as before.
 */
export type StepEvent = {
	type: 'step';
	node: string;
	label: string;
	phase?: 'start' | 'end';
	thought?: string | null;
};

/**
 * The query the agent is about to run, sent once it has cleared syntax and
 * intent validation, so the UI can show it while the database works rather
 * than only alongside the final answer. Drafts that validation rejects are
 * never sent, so what arrives here is always what executes. A query that
 * fails at execution and is rebuilt sends a new event; the newest one wins.
 */
export type SqlEvent = {
	type: 'sql';
	node: string;
	sql: string;
};

/** Shape of the executed SQL result, as returned by `sql_response_from_db`. */
export type SqlResult = string[] | { [key: string]: string }[];

export type ResultEvent = {
	type: 'result';
	answer: {
		response: string;
		sql_code?: string;
		sql_response_from_db?: SqlResult;
		thoughts?: string;
	};
};

export type ErrorEvent = {
	type: 'error';
	message: string;
	/** HTTP status when the request itself was refused before streaming. */
	status?: number;
};

/**
 * Message 2 — the chart or fallback result table, generated and persisted
 * server-side (see `auto_ontology/server/chat/router.py`'s `_pump`) right after the SQL
 * answer, using the same guarantee as Message 1: it exists whether or not a
 * browser tab is still around to ask for it. Omitted entirely when there was
 * no executed result to visualize.
 */
export type ChartsEvent = {
	type: 'charts';
	content: string;
	sql_response?: string | null;
};

/** One choice offered by a clarification question. */
export type ClarificationOption = {
	id: string;
	label: string;
	description?: string;
};

/**
 * The backend needs a business term defined before it can write the SQL
 * ('VIP customers'). The stream ends normally after this event; the answer
 * comes back as the next turn on the same conversation.
 */
export type ClarificationEvent = {
	type: 'clarification';
	clarification_id: string;
	term: string;
	question: string;
	options: ClarificationOption[];
	allow_other: boolean;
};

/** The answer, as sent in the next request: a chosen option or free text. */
export type ClarificationReply =
	| { clarification_id: string; option_id: string; text?: undefined }
	| { clarification_id: string; text: string; option_id?: undefined };

/** What the thread keeps of a clarification: the question and, once given, the answer. */
export type ClarificationPart = {
	id: string;
	term: string;
	question: string;
	options: ClarificationOption[];
	allowOther: boolean;
	/** Absent while the question is waiting for an answer. */
	answer?: { optionId?: string; text: string };
	/** The backend refused answers to it (stale or already answered). */
	closed?: boolean;
};

export type ChatStreamEvent =
	| StepEvent
	| SqlEvent
	| ResultEvent
	| ErrorEvent
	| ChartsEvent
	| ClarificationEvent;

export type GraphStep = {
	node: string;
	label: string;
	thought?: string | null;
	status: 'completed' | 'active';
};

export type Conversation = {
	id: string;
	title: string;
	messages: ChatMessage[];
	createdAt: number;
};
