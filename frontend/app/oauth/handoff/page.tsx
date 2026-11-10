// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { Suspense, useEffect } from 'react';
import { useSearchParams } from 'next/navigation';
import { Icon, IconName } from '@/common/icons';
import { deliverLoopbackCallback, isLoopbackCallback } from '@/auth/oauth-loopback';
import { OauthStatus } from '@/app/oauth/OauthStatus';

// The page is public and takes its target from the query string, so it only
// ever talks to a loopback listener. Anything else is refused rather than
// navigated to; the server-side rewrite never sends other URLs here.
const callbackError = (target: string | null): string | null => {
	if (!target) return 'Missing callback.';
	try {
		if (!isLoopbackCallback(new URL(target))) return 'Invalid callback.';
	} catch {
		return 'Invalid callback.';
	}
	return null;
};

const HandoffStatus = () => {
	const params = useSearchParams();
	const target = params.get('url');
	const error = callbackError(target);

	useEffect(() => {
		if (error || !target) return undefined;
		void deliverLoopbackCallback(target);
		return undefined;
	}, [error, target]);

	if (error) {
		return <p className="text-center text-sm text-red-500">{error}</p>;
	}

	return (
		<OauthStatus title="Thank You">
			Authentication successful. You can close this page.
		</OauthStatus>
	);
};

const HandoffPage = () => (
	<div className="flex h-full w-full items-center justify-center p-6">
		<div className="glass flex w-full max-w-[420px] flex-col items-center gap-6 rounded-[28px] border border-white/80 p-9 dark:border-white/10">
			<div className="flex items-center gap-2">
				<span className="flex h-11 w-11 items-center justify-center rounded-[14px] bg-primary">
					<Icon name={IconName.AppLogo} className="h-6 w-6 text-white" />
				</span>
				<span className="text-lg font-bold text-glass-text dark:text-zinc-50">
					Auto Ontology
				</span>
			</div>
			<Suspense fallback={null}>
				<HandoffStatus />
			</Suspense>
		</div>
	</div>
);

export default HandoffPage;
