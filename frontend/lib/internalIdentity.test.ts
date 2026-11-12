// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { proxyToBackend } from '@/auth/proxy-backend';
import {
	buildInternalHeaders,
	INTERNAL_TOKEN_HEADER,
	INTERNAL_USER_HEADER,
} from '@/lib/internalIdentity';

// Obviously fake.
const TOKEN = 'test-only-token-0123456789-abcdefghijklmnop';

afterEach(() => {
	vi.unstubAllEnvs();
	vi.unstubAllGlobals();
});

describe('buildInternalHeaders', () => {
	it('sends the token and the identity when both are known', () => {
		vi.stubEnv('INTERNAL_API_TOKEN', TOKEN);
		expect(buildInternalHeaders('user-1')).toEqual({
			[INTERNAL_TOKEN_HEADER]: TOKEN,
			[INTERNAL_USER_HEADER]: 'user-1',
		});
	});

	it('sends the token alone for a call with no user', () => {
		vi.stubEnv('INTERNAL_API_TOKEN', TOKEN);
		expect(buildInternalHeaders()).toEqual({ [INTERNAL_TOKEN_HEADER]: TOKEN });
	});

	it('omits the token when none is configured, so existing deployments keep working', () => {
		vi.stubEnv('INTERNAL_API_TOKEN', '');
		expect(buildInternalHeaders('user-1')).toEqual({ [INTERNAL_USER_HEADER]: 'user-1' });
		expect(buildInternalHeaders()).toEqual({});
	});
});

describe('proxyToBackend', () => {
	const relay = async (init: RequestInit, userId?: string) => {
		const fetchMock = vi.fn(async () => new Response('{}', { status: 200 }));
		vi.stubGlobal('fetch', fetchMock);
		await proxyToBackend(new Request('http://web/api/terms', init), { userId });
		const [, sent] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
		return sent.headers as Record<string, string>;
	};

	it('attaches the token and the resolved identity', async () => {
		vi.stubEnv('INTERNAL_API_TOKEN', TOKEN);
		const headers = await relay({}, 'user-1');
		expect(headers[INTERNAL_TOKEN_HEADER]).toBe(TOKEN);
		expect(headers[INTERNAL_USER_HEADER]).toBe('user-1');
	});

	it('does not relay a token or identity the browser supplied', async () => {
		vi.stubEnv('INTERNAL_API_TOKEN', TOKEN);
		const headers = await relay({
			headers: { [INTERNAL_TOKEN_HEADER]: 'forged', [INTERNAL_USER_HEADER]: 'admin' },
		});
		expect(headers[INTERNAL_TOKEN_HEADER]).toBe(TOKEN);
		expect(headers[INTERNAL_USER_HEADER]).toBeUndefined();
	});
});

// Every server-side path to the Python services must carry the token. There is
// no single choke point (route handlers use fetch, server components use the
// axios instance), so this reads the sources: a new file that targets the
// Python API without the shared helper fails here, instead of failing in
// production the day an operator turns the token on.
describe('server-side callers of the Python services', () => {
	const root = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..');
	const skip = new Set(['node_modules', '.next', 'scripts', 'public']);

	const sources = (dir: string): string[] =>
		readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
			const full = path.join(dir, entry.name);
			if (entry.isDirectory()) return skip.has(entry.name) ? [] : sources(full);
			return /\.(ts|tsx)$/.test(entry.name) && !entry.name.endsWith('.test.ts') ? [full] : [];
		});

	const files = sources(root).map((file) => ({
		name: path.relative(root, file).split(path.sep).join('/'),
		text: readFileSync(file, 'utf8'),
	}));

	it('finds the callers it is meant to guard', () => {
		const callers = files.filter((f) => f.text.includes('PYTHON_API_URL')).map((f) => f.name);
		expect(callers).toEqual(
			expect.arrayContaining([
				'api/requests.ts',
				'auth/proxy-backend.ts',
				'app/api/chat/completions/route.ts',
			]),
		);
	});

	it('uses buildInternalHeaders in every file that targets the Python API or ingestion service', () => {
		const missing = files
			.filter((f) => /PYTHON_API_URL|INGESTION_SERVICE_URL/.test(f.text))
			.filter((f) => !f.text.includes('buildInternalHeaders'))
			.map((f) => f.name);
		expect(missing).toEqual([]);
	});

	it('keeps axios behind api/requests.ts, the only axios instance that attaches the token', () => {
		const importers = files
			.filter((f) => /from 'axios'/.test(f.text))
			.map((f) => f.name)
			.filter((name) => name !== 'api/requests.ts');
		expect(importers).toEqual([]);
	});
});
