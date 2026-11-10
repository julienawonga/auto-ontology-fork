// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { Icon, IconName } from '@/common/icons';
import { getOpenInvitation } from '@/lib/invitations';
import { InviteSetPassword } from './InviteSetPassword';

const InviteInvalid = () => (
	<div className="flex h-full w-full items-center justify-center p-6">
		<div className="glass flex w-full max-w-[420px] flex-col items-center gap-4 rounded-[28px] border border-white/80 p-9 text-center dark:border-white/10">
			<div className="flex items-center gap-2">
				<span className="flex h-11 w-11 items-center justify-center rounded-[14px] bg-primary">
					<Icon name={IconName.AppLogo} className="h-6 w-6 text-white" />
				</span>
				<span className="text-lg font-bold text-glass-text dark:text-zinc-50">Lexis</span>
			</div>
			<p className="text-sm text-body dark:text-zinc-300">
				This invitation is invalid or has expired.
			</p>
		</div>
	</div>
);

const InvitePage = async ({ params }: { params: Promise<{ token: string }> }) => {
	const { token } = await params;
	const invitation = await getOpenInvitation(token);
	if (!invitation) return <InviteInvalid />;
	return <InviteSetPassword token={token} email={invitation.email} kind={invitation.kind} />;
};

export default InvitePage;
