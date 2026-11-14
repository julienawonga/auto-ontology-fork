// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import { formatGenerationButtonText, getBannerHelpText } from './DomainGenerationBanner';

describe('domain generation banner pure helpers', () => {
	it('formats button text based on generating state', () => {
		expect(formatGenerationButtonText(false)).toBe(
			"Group this database's Terms by business domain",
		);
		expect(formatGenerationButtonText(true)).toBe('Grouping Terms... (this may take 10-60s)');
	});

	it('returns the correct help text', () => {
		expect(getBannerHelpText()).toBe(
			"Group this database's Terms into business domains for a higher-level view.",
		);
	});
});
