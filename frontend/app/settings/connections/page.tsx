// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { redirect } from 'next/navigation';
import { ConnectionsView } from '@/components/connectionsPage/ConnectionsView';
import { readConnectionsEnvSource } from '@/lib/server/connections';

export default async function ConnectionsSettingsPage() {
	// Connections from CONNECTION_STRINGS are fixed by config and cannot be
	// created, edited or removed here, which is why SettingsNav hides this
	// section. Hiding the link is not enough on its own: a bookmark or a typed
	// URL would still reach a page whose every action is a dead end.
	const envManaged = await readConnectionsEnvSource();
	if (envManaged === true) {
		redirect('/settings/zones');
	}

	return (
		<main className="min-h-0 min-w-0 flex-1 overflow-y-auto px-7 py-6 sm:px-10 sm:py-7">
			<ConnectionsView />
		</main>
	);
}
