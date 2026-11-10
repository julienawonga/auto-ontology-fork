// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

import { NextResponse } from 'next/server';
import { getPrisma } from '@/lib/prisma';
import { CHAT_DETAILS_EXPANDED_KEY, isChatDetailsExpanded } from '@/lib/configurations';
import { withPermission } from '@/auth/with-auth';

// Report whether chat answers show their explanation and SQL expanded.
//
// Readable by anyone who can chat, since every chat page needs it to render
// answers; the flag is display-only and reveals nothing about the data.
export const GET = withPermission({ chat: ['use'] })(async () => {
	return NextResponse.json({ enabled: await isChatDetailsExpanded() });
});

// Turn the instance-wide "Expand Reasoning and SQL" setting on or off
// (admin-only). It sits on Settings > Agent Settings next to "Visualize SQL
// Results", so it is guarded by the same permission. Open chat pages pick the
// new value up the next time they load it.
export const PUT = withPermission({ visualization: ['manage'] })(async (req) => {
	const prisma = getPrisma();
	const body = await req.json();
	const enabled = body.enabled === true;
	const value = enabled ? 'true' : 'false';

	await prisma.configuration.upsert({
		where: { key: CHAT_DETAILS_EXPANDED_KEY },
		create: { key: CHAT_DETAILS_EXPANDED_KEY, value },
		update: { value },
	});

	return NextResponse.json({ enabled });
});
