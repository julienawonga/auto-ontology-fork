// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { requests } from './requests';
import type { ResponseWithError } from './types';
import type {
	SemanticCompilationStatus,
	SemanticReindexOutcome,
	SemanticReindexResult,
} from '@/types/semanticIndex';

export type Prompt = {
	id: string;
	content: string;
};

export type Acronym = {
	id: string;
	name: string;
	description: string;
	created_at: string;
	updated_at: string;
};

async function json<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
	const res = await fetch(input, init);
	if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
	return res.json() as Promise<T>;
}

export const promptsApi = {
	get: () => json<Prompt[]>('/api/custom-prompts'),

	create: (data: { content?: string } = {}) =>
		json<Prompt>('/api/custom-prompts', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(data),
		}),

	update: (id: string, data: { content?: string }) =>
		json<Prompt>(`/api/custom-prompts/${id}`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(data),
		}),

	delete: (id: string) => fetch(`/api/custom-prompts/${id}`, { method: 'DELETE' }),
};

export const acronymsApi = {
	get: () => json<Acronym[]>('/api/acronyms'),

	checkName: (name: string) => json<{ exists: boolean }>(`/api/acronyms?name=${name}`),

	create: (data: { name: string; description?: string }) =>
		json<Acronym>('/api/acronyms', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(data),
		}),

	update: (id: string, data: { name?: string; description?: string }) =>
		json<Acronym>(`/api/acronyms/${id}`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(data),
		}),

	delete: (id: string) => fetch(`/api/acronyms/${id}`, { method: 'DELETE' }),
};

export const semanticCompilationApi = {
	get: () => json<{ enabled: boolean }>('/api/configurations/semantic-compilation'),

	getStatus: () => json<SemanticCompilationStatus>('/api/semantic-compilation/status'),

	setEnabled: (enabled: boolean) =>
		json<{ enabled: boolean }>('/api/configurations/semantic-compilation', {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ enabled }),
		}),

	reset: () => json<{ status: string }>('/api/semantic-compilation/reset', { method: 'POST' }),

	// Plain fetch rather than `json`: the page explains 404/409/503 differently, so the
	// status has to survive. Network failures still reject.
	reindex: async (databaseName: string): Promise<SemanticReindexOutcome> => {
		const res = await fetch(
			`/api/semantic-compilation/reindex?database_name=${encodeURIComponent(databaseName)}`,
			{ method: 'POST' },
		);
		if (res.ok) return { ok: true, result: (await res.json()) as SemanticReindexResult };
		const body: unknown = await res.json().catch(() => null);
		const detail =
			body !== null && typeof body === 'object' && 'detail' in body
				? (body as { detail: unknown }).detail
				: null;
		return {
			ok: false,
			status: res.status,
			detail: typeof detail === 'string' ? detail : null,
		};
	},
};

export const distinctValueProbingApi = {
	get: () => json<{ enabled: boolean }>('/api/configurations/distinct-value-probing'),

	setEnabled: (enabled: boolean) =>
		json<{ enabled: boolean }>('/api/configurations/distinct-value-probing', {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ enabled }),
		}),
};

export const piiDetectionApi = {
	get: () => json<{ enabled: boolean }>('/api/configurations/pii-detection'),

	setEnabled: (enabled: boolean) =>
		json<{ enabled: boolean }>('/api/configurations/pii-detection', {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ enabled }),
		}),
};

// Instance-wide "Visualize SQL Results" toggle (Settings > Agent Settings).
export const visualizationApi = {
	get: (): Promise<ResponseWithError<{ enabled: boolean }>> =>
		requests.get('configurations/visualization'),

	setEnabled: (enabled: boolean): Promise<ResponseWithError<{ enabled: boolean }>> =>
		requests.put('configurations/visualization', { enabled }),
};

// Instance-wide "Expand Reasoning and SQL" toggle (Settings > Agent Settings).
export const chatDetailsApi = {
	get: (): Promise<ResponseWithError<{ enabled: boolean }>> =>
		requests.get('configurations/chat-details'),

	setEnabled: (enabled: boolean): Promise<ResponseWithError<{ enabled: boolean }>> =>
		requests.put('configurations/chat-details', { enabled }),
};

// Instance-wide timeout for SQL the text-to-SQL agent runs (Settings > Agent Settings).
export const sqlQueryTimeoutApi = {
	get: (): Promise<ResponseWithError<{ seconds: number }>> =>
		requests.get('configurations/sql-query-timeout'),

	set: (seconds: number): Promise<ResponseWithError<{ seconds: number }>> =>
		requests.put('configurations/sql-query-timeout', { seconds }),
};

// Instance-wide cap on rows one agent-issued SQL statement may return (Settings > Agent Settings).
export const sqlResultMaxRowsApi = {
	get: (): Promise<ResponseWithError<{ rows: number }>> =>
		requests.get('configurations/sql-result-max-rows'),

	set: (rows: number): Promise<ResponseWithError<{ rows: number }>> =>
		requests.put('configurations/sql-result-max-rows', { rows }),
};
