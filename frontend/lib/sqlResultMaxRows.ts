// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

/**
 * Default and bounds of the SQL Result Row Limit setting (Settings > Agent
 * Settings). They mirror `DEFAULT_`/`MIN_`/`MAX_SQL_RESULT_MAX_ROWS` in
 * `auto_ontology/infra/feature_flags.py`, which reads the stored value before
 * every statement the text-to-SQL agent issues.
 *
 * Kept free of Prisma and Next-runtime imports: the client settings page and
 * the OpenAPI generator both import it.
 */

export const DEFAULT_SQL_RESULT_MAX_ROWS = 10_000;
export const MIN_SQL_RESULT_MAX_ROWS = 1;
export const MAX_SQL_RESULT_MAX_ROWS = 1_000_000;

export function isValidSqlResultMaxRows(rows: number): boolean {
	return (
		Number.isInteger(rows) && rows >= MIN_SQL_RESULT_MAX_ROWS && rows <= MAX_SQL_RESULT_MAX_ROWS
	);
}

/**
 * Read the stored limit the way the Python reader does: a missing row, a
 * non-integer, or a value outside the bounds all mean the default, so the page
 * never shows a number the backend is not actually using.
 */
export function readSqlResultMaxRows(value: string | null | undefined): number {
	const trimmed = value?.trim() ?? '';
	if (!/^[+-]?\d+$/.test(trimmed)) return DEFAULT_SQL_RESULT_MAX_ROWS;
	const rows = Number(trimmed);
	return isValidSqlResultMaxRows(rows) ? rows : DEFAULT_SQL_RESULT_MAX_ROWS;
}
