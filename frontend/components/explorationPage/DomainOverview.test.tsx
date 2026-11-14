// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import {
	calculateCertifiedPercentage,
	domainBubbleSize,
	domainLinkWidth,
	formatCertifiedText,
	formatLinkLabel,
	getTopNeighbours,
} from './DomainOverview';
import type { SemanticDomain, SemanticDomainLink } from '@/types/exploration';

describe('DomainOverview pure helpers', () => {
	it('domainBubbleSize computes clamped diameter based on term count', () => {
		expect(domainBubbleSize(0)).toBe(72);
		expect(domainBubbleSize(1)).toBe(72);
		expect(domainBubbleSize(100)).toBe(160);
		expect(domainBubbleSize(1000)).toBe(220);
	});

	it('domainLinkWidth computes line stroke width based on max weight', () => {
		expect(domainLinkWidth(1, 1)).toBe(1);
		expect(domainLinkWidth(5, 10)).toBe(4.111111111111111);
		expect(domainLinkWidth(10, 10)).toBe(8);
	});

	it('formatLinkLabel formats weight and shared tables count', () => {
		expect(formatLinkLabel(5, 3)).toBe('5 links · 3 tables');
		expect(formatLinkLabel(1, 0)).toBe('1 links · 0 tables');
	});

	it('formatCertifiedText formats certified count out of total terms', () => {
		expect(formatCertifiedText(3, 10)).toBe('3 of 10 certified');
		expect(formatCertifiedText(0, 5)).toBe('0 of 5 certified');
	});

	it('calculateCertifiedPercentage calculates rounded percentage', () => {
		expect(calculateCertifiedPercentage(3, 10)).toBe(30);
		expect(calculateCertifiedPercentage(1, 3)).toBe(33);
		expect(calculateCertifiedPercentage(0, 0)).toBe(0);
	});

	it('getTopNeighbours finds top connected domains sorted by weight', () => {
		const domains: SemanticDomain[] = [
			{ id: 'd1', name: 'Finance', term_ids: ['t1'], term_count: 5 },
			{ id: 'd2', name: 'Sales', term_ids: ['t2'], term_count: 3 },
			{ id: 'd3', name: 'Marketing', term_ids: ['t3'], term_count: 2 },
			{ id: 'd4', name: 'HR', term_ids: ['t4'], term_count: 1 },
		];

		const links: SemanticDomainLink[] = [
			{ source: 'd1', target: 'd2', weight: 10, shared_table_count: 2 },
			{ source: 'd1', target: 'd3', weight: 5, shared_table_count: 1 },
			{ source: 'd1', target: 'd4', weight: 2, shared_table_count: 1 },
		];

		const top = getTopNeighbours('d1', domains, links, 2);
		expect(top.length).toBe(2);
		expect(top[0].domain.id).toBe('d2');
		expect(top[0].weight).toBe(10);
		expect(top[1].domain.id).toBe('d3');
		expect(top[1].weight).toBe(5);
	});
});
