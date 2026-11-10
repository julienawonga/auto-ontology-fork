// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import { describeIndexEntry, describeReindexResult, reindexErrorMessage } from './semanticIndex';

describe('describeIndexEntry', () => {
	it('reports a complete index', () => {
		expect(
			describeIndexEntry({ database_name: 'a', expected: 10, indexed: 10, missing: 0 }),
		).toEqual({ complete: true, label: 'Complete (10 indexed)' });
	});

	it('warns when entries are missing', () => {
		expect(
			describeIndexEntry({ database_name: 'a', expected: 10, indexed: 4, missing: 6 }),
		).toEqual({ complete: false, label: '6 of 10 missing' });
	});

	it('does not call an empty database missing', () => {
		expect(
			describeIndexEntry({ database_name: 'a', expected: 0, indexed: 0, missing: 0 }),
		).toEqual({ complete: true, label: 'Nothing to index' });
	});
});

describe('describeReindexResult', () => {
	it('is complete when everything was written', () => {
		const r = describeReindexResult({ database_name: 'a', expected: 5, written: 5 });
		expect(r.complete).toBe(true);
		expect(r.message).toContain('5 of 5');
	});

	it('flags a partial rebuild distinctly', () => {
		const r = describeReindexResult({ database_name: 'a', expected: 5, written: 2 });
		expect(r.complete).toBe(false);
		expect(r.message).toContain('only 2 of 5');
	});
});

describe('reindexErrorMessage', () => {
	it('explains the handled statuses', () => {
		expect(reindexErrorMessage(404, null)).toMatch(/no longer known/);
		expect(reindexErrorMessage(409, null)).toMatch(/compilation is running/);
		expect(reindexErrorMessage(503, null)).toMatch(/not configured/);
	});

	it('falls back to the backend detail, then to a generic text', () => {
		expect(reindexErrorMessage(500, 'boom')).toBe('boom');
		expect(reindexErrorMessage(500, null)).toMatch(/Failed to rebuild/);
	});
});
