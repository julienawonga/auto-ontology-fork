// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

import { NextResponse } from 'next/server';
import { getPrisma } from '@/lib/prisma';
import { SQL_RESULT_MAX_ROWS_KEY } from '@/lib/configurations';
import {
	MAX_SQL_RESULT_MAX_ROWS,
	MIN_SQL_RESULT_MAX_ROWS,
	readSqlResultMaxRows,
} from '@/lib/sqlResultMaxRows';
import { withPermission } from '@/auth/with-auth';
import { sqlResultMaxRowsBody } from './openapi';

// Key/value stored in the `configurations` table. The text-to-SQL pipeline
// reads this same key (see auto_ontology/infra/feature_flags.py) for each
// statement it issues and cuts the result at this many rows. An absent row
// means 10,000, which is also what a backend running without this frontend
// uses.

// Report the instance-wide SQL result row limit.
export const GET = withPermission({ visualization: ['read'] })(async () => {
	const row = await getPrisma().configuration.findUnique({
		where: { key: SQL_RESULT_MAX_ROWS_KEY },
	});
	return NextResponse.json({ rows: readSqlResultMaxRows(row?.value) });
});

// Set the instance-wide SQL result row limit (admin-only).
//
// Rejects anything but an in-range integer rather than coercing it: the backend
// treats an unusable stored value as the default, so a coerced value would be
// silently ignored while the page showed it as saved.
export const PUT = withPermission({ visualization: ['manage'] })(async (req) => {
	let raw: unknown;
	try {
		raw = await req.json();
	} catch {
		return NextResponse.json({ error: 'Body must be JSON.' }, { status: 400 });
	}

	const parsed = sqlResultMaxRowsBody.safeParse(raw);
	if (!parsed.success) {
		return NextResponse.json(
			{
				error: `Row limit must be a whole number from ${MIN_SQL_RESULT_MAX_ROWS} to ${MAX_SQL_RESULT_MAX_ROWS}.`,
			},
			{ status: 400 },
		);
	}

	const { rows } = parsed.data;
	const value = String(rows);

	await getPrisma().configuration.upsert({
		where: { key: SQL_RESULT_MAX_ROWS_KEY },
		create: { key: SQL_RESULT_MAX_ROWS_KEY, value },
		update: { value },
	});

	return NextResponse.json({ rows });
});
