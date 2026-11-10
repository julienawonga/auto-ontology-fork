// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

/** One database's semantic index coverage, from `GET /api/semantic-compilation/status`. */
export type SemanticIndexEntry = {
	database_name: string;
	/** Terms and ColumnAttributes the relational store holds for the database. */
	expected: number;
	indexed: number;
	missing: number;
};

export type SemanticCompilationStatus = {
	calculated: boolean;
	running: boolean;
	last_success_at: string | null;
	last_failure_at: string | null;
	/** Absent on backends that predate the index report. */
	index?: SemanticIndexEntry[];
};

/** Body of `POST /api/semantic-compilation/reindex`. */
export type SemanticReindexResult = {
	database_name: string;
	expected: number;
	written: number;
};

/** Outcome of a reindex call, with the HTTP status kept so errors can be explained. */
export type SemanticReindexOutcome =
	| { ok: true; result: SemanticReindexResult }
	| { ok: false; status: number; detail: string | null };
