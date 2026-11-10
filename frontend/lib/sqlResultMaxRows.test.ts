// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import {
	DEFAULT_SQL_RESULT_MAX_ROWS,
	MAX_SQL_RESULT_MAX_ROWS,
	MIN_SQL_RESULT_MAX_ROWS,
	isValidSqlResultMaxRows,
	readSqlResultMaxRows,
} from './sqlResultMaxRows';

// The bounds mirror auto_ontology/infra/feature_flags.py; pin them so a one-sided edit fails.
describe('sql result row limit bounds', () => {
	it('mirrors the backend constants', () => {
		expect(DEFAULT_SQL_RESULT_MAX_ROWS).toBe(10_000);
		expect(MIN_SQL_RESULT_MAX_ROWS).toBe(1);
		expect(MAX_SQL_RESULT_MAX_ROWS).toBe(1_000_000);
	});

	it('accepts only integers inside the bounds', () => {
		expect(isValidSqlResultMaxRows(1)).toBe(true);
		expect(isValidSqlResultMaxRows(1_000_000)).toBe(true);
		expect(isValidSqlResultMaxRows(0)).toBe(false);
		expect(isValidSqlResultMaxRows(1_000_001)).toBe(false);
		expect(isValidSqlResultMaxRows(1.5)).toBe(false);
		expect(isValidSqlResultMaxRows(Number.NaN)).toBe(false);
	});

	it('reads junk or out-of-range stored values as the default', () => {
		expect(readSqlResultMaxRows('250')).toBe(250);
		expect(readSqlResultMaxRows(' 250 ')).toBe(250);
		expect(readSqlResultMaxRows(undefined)).toBe(DEFAULT_SQL_RESULT_MAX_ROWS);
		expect(readSqlResultMaxRows('abc')).toBe(DEFAULT_SQL_RESULT_MAX_ROWS);
		expect(readSqlResultMaxRows('0')).toBe(DEFAULT_SQL_RESULT_MAX_ROWS);
		expect(readSqlResultMaxRows('2000000')).toBe(DEFAULT_SQL_RESULT_MAX_ROWS);
	});
});
