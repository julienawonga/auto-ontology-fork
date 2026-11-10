// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

/**
 * A small SQL tokenizer for read-only code blocks.
 *
 * It only tells keywords, strings, numbers and comments apart, which is enough
 * to make a query scannable; it does not parse. Every character of the input
 * ends up in exactly one token, so joining the texts gives the input back.
 */

export type SqlTokenKind = 'keyword' | 'string' | 'number' | 'comment' | 'plain';

export type SqlToken = { text: string; kind: SqlTokenKind };

const KEYWORDS = new Set([
	'select',
	'from',
	'where',
	'and',
	'or',
	'not',
	'in',
	'is',
	'null',
	'as',
	'on',
	'join',
	'inner',
	'left',
	'right',
	'full',
	'outer',
	'cross',
	'group',
	'by',
	'order',
	'having',
	'limit',
	'offset',
	'distinct',
	'union',
	'all',
	'with',
	'case',
	'when',
	'then',
	'else',
	'end',
	'asc',
	'desc',
	'between',
	'like',
	'ilike',
	'exists',
	'over',
	'partition',
	'count',
	'sum',
	'avg',
	'min',
	'max',
	'coalesce',
	'cast',
	'true',
	'false',
	'using',
	'insert',
	'into',
	'values',
	'update',
	'set',
	'delete',
	'predict',
]);

// Order matters: comments and strings first, so their contents are not
// split into words.
const TOKEN =
	/(--[^\n]*|\/\*[\s\S]*?\*\/)|('(?:[^']|'')*'?)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|([\s\S])/g;

export const tokenizeSql = (sql: string): SqlToken[] => {
	const tokens: SqlToken[] = [];
	const push = (text: string, kind: SqlTokenKind) => {
		const last = tokens[tokens.length - 1];
		// Merge runs of plain text so a long query renders few nodes.
		if (kind === 'plain' && last?.kind === 'plain') last.text += text;
		else tokens.push({ text, kind });
	};
	for (const match of sql.matchAll(TOKEN)) {
		const [text, comment, string, number, word] = match;
		if (comment) push(text, 'comment');
		else if (string) push(text, 'string');
		else if (number) push(text, 'number');
		else if (word) push(text, KEYWORDS.has(word.toLowerCase()) ? 'keyword' : 'plain');
		else push(text, 'plain');
	}
	return tokens;
};
