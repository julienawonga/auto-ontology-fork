// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { invitationsApi } from '@/api/invitations';
import { authApi } from '@/api/auth';
import { Button } from '@/common/Button';
import { Icon, IconName } from '@/common/icons';
import { Size, ButtonTheme } from '@/enums/button';
import { InvitationKind } from '@/enums/invitation';

const inputClass =
	'w-full rounded-xl border border-zinc-900/15 bg-white/70 px-3 py-2 text-sm text-body outline-none transition-colors placeholder:text-secondary focus:border-primary focus:ring-2 focus:ring-primary/30 dark:border-white/15 dark:bg-white/[0.06] dark:text-zinc-300';

const MIN_PASSWORD_LENGTH = 8;
const MAX_PASSWORD_LENGTH = 128;

type InviteSetPasswordProps = {
	token: string;
	email: string;
	kind: InvitationKind;
};

export const InviteSetPassword = ({ token, email, kind }: InviteSetPasswordProps) => {
	const router = useRouter();
	const [password, setPassword] = useState('');
	const [confirm, setConfirm] = useState('');
	const [error, setError] = useState<string | null>(null);
	const [submitting, setSubmitting] = useState(false);
	const [savedWithoutSession, setSavedWithoutSession] = useState(false);

	const handleSubmit = async (event: React.FormEvent) => {
		event.preventDefault();
		if (password !== confirm) {
			setError('Passwords do not match.');
			return;
		}
		if (password.length < MIN_PASSWORD_LENGTH) {
			setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
			return;
		}
		if (password.length > MAX_PASSWORD_LENGTH) {
			setError(`Password must be at most ${MAX_PASSWORD_LENGTH} characters.`);
			return;
		}

		setSubmitting(true);
		setError(null);
		const accepted = await invitationsApi.accept(token, password);
		if (accepted.error) {
			setError(accepted.message ?? 'Could not accept the invitation.');
			setSubmitting(false);
			return;
		}

		const signedIn = await authApi.signInWithPassword(email, password);
		if (signedIn.error) {
			setSavedWithoutSession(true);
			setSubmitting(false);
			return;
		}

		router.push('/chat');
		router.refresh();
	};

	return (
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
				{savedWithoutSession ? (
					<div className="flex w-full flex-col gap-4">
						<p className="text-sm text-body dark:text-zinc-300">
							{kind === InvitationKind.Reset
								? 'Password updated. Sign in to continue.'
								: 'Account created. Sign in to continue.'}
						</p>
						<Button
							theme={ButtonTheme.Primary}
							size={Size.REGULAR}
							type="button"
							onClick={() => router.push('/login')}
						>
							Sign in
						</Button>
					</div>
				) : (
					<form
						onSubmit={(event) => void handleSubmit(event)}
						className="flex w-full flex-col gap-4"
					>
						<p className="text-xs text-secondary dark:text-zinc-400">
							{kind === InvitationKind.Reset
								? 'Set a new password for '
								: 'Set a password for '}
							<span className="font-medium text-body">{email}</span>
							{kind === InvitationKind.Reset ? '.' : ' to finish joining.'}
						</p>
						<div className="flex flex-col gap-1.5">
							<label
								htmlFor="invite-email"
								className="text-xs font-medium text-body dark:text-zinc-400"
							>
								Email
							</label>
							<input
								id="invite-email"
								type="email"
								readOnly
								value={email}
								className={`${inputClass} cursor-not-allowed bg-zinc-50 dark:bg-zinc-800/60`}
							/>
						</div>
						<div className="flex flex-col gap-1.5">
							<label
								htmlFor="invite-password"
								className="text-xs font-medium text-body dark:text-zinc-400"
							>
								Password
							</label>
							<input
								id="invite-password"
								type="password"
								required
								minLength={MIN_PASSWORD_LENGTH}
								maxLength={MAX_PASSWORD_LENGTH}
								autoComplete="new-password"
								value={password}
								onChange={(event) => setPassword(event.target.value)}
								className={inputClass}
							/>
						</div>
						<div className="flex flex-col gap-1.5">
							<label
								htmlFor="invite-password-confirm"
								className="text-xs font-medium text-body dark:text-zinc-400"
							>
								Confirm password
							</label>
							<input
								id="invite-password-confirm"
								type="password"
								required
								minLength={MIN_PASSWORD_LENGTH}
								maxLength={MAX_PASSWORD_LENGTH}
								autoComplete="new-password"
								value={confirm}
								onChange={(event) => setConfirm(event.target.value)}
								className={inputClass}
							/>
						</div>
						{error ? <p className="text-xs text-red-500">{error}</p> : null}
						<Button
							theme={ButtonTheme.Primary}
							size={Size.REGULAR}
							type="submit"
							disabled={submitting}
						>
							{submitting
								? 'Saving…'
								: kind === InvitationKind.Reset
									? 'Save password'
									: 'Create password'}
						</Button>
					</form>
				)}
			</div>
		</div>
	);
};
